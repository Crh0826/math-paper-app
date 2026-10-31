# -*- coding: utf-8 -*-
"""t11 · 删卷策略（队长拍板）：没判过卷 → 硬删；判过卷 → 只软删、保留判卷证据。

守的事：
  1. n==0：删 papers 行 + 删 out/<pid> 目录；
  2. n>0：只置 deleted_at，**目录与 submissions 一行不动**（裁切图/annotated 是"为什么判错"的证据）；
  3. 软删的卷子：列表里看不到、预览与 PDF 走 410 友好页；
  4. 列表每行显示「判卷 N 次」；/healthz 单列 papers_with_submissions_mb。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import build_db  # noqa: E402
import db as dbmod  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def add_paper(db_path: str, out_dir: str, pid: str, title: str = "样例卷",
              created_at: str = "2026-10-31T15:00:00+08:00",
              submissions: int = 0, payload_bytes: int = 2048) -> str:
    folder = os.path.join(out_dir, pid)
    os.makedirs(folder, exist_ok=True)
    items = [{"idx": 1, "no": "00001", "qtype": "选择", "kp_id": "1.1",
              "answer_key": [{"blank": 1, "value": "A"}]}]
    manifest = {"pid": pid, "title": title, "created_at": created_at, "items": items,
                "card": {"n_pages": 1}}
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest,"
            " docx_path, pdf_path, n_items) VALUES(?,?,?,?,?,?,?,?)",
            (pid, created_at, title, json.dumps({"title": title}),
             json.dumps(manifest, ensure_ascii=False),
             os.path.join(folder, "paper.docx"), os.path.join(folder, "paper.pdf"), 1))
        for i in range(submissions):
            sid = "S%s-%02d" % (pid[1:], i + 1)
            conn.execute(
                "INSERT OR REPLACE INTO submissions(sid,pid,created_at,image_path,"
                " warp_ok,qr_ok,score,full,detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, pid, created_at, os.path.join(folder, "sub_%s" % sid, "source.jpg"),
                 1, 1, 1.0, 2.0, json.dumps({"items": []})))
            os.makedirs(os.path.join(folder, "sub_%s" % sid), exist_ok=True)
            with open(os.path.join(folder, "sub_%s" % sid, "1-1.png"), "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"x" * 128)
        conn.commit()
    finally:
        conn.close()
    with open(os.path.join(folder, "paper.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4\n%%EOF\n")
    with open(os.path.join(folder, "card.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4\n%%EOF\n")
    with open(os.path.join(folder, "evidence.bin"), "wb") as fh:
        fh.write(b"x" * payload_bytes)
    return folder


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="softdel_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=60, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
        cls.app = server.create_app(
            db_path=cls.db_path,
            config_path=os.path.join(cls.tmp, "none.json"),
            out_dir=cls.out_dir,
        )
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def row(self, pid):
        conn = dbmod.connect(self.db_path)
        try:
            r = conn.execute("SELECT * FROM papers WHERE pid=?", (pid,)).fetchone()
        finally:
            conn.close()
        return dict(r) if r else None

    def submissions(self, pid):
        conn = dbmod.connect(self.db_path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM submissions WHERE pid=?",
                                    (pid,)).fetchone()[0])
        finally:
            conn.close()


class TestHardDelete(_Base):
    def test_paper_without_submissions_is_hard_deleted(self):
        pid = "P20261031-201"
        folder = add_paper(self.db_path, self.out_dir, pid, "纯草稿卷", submissions=0)
        r = self.client.post("/papers/%s/delete" % pid, follow_redirects=False)
        self.assertEqual(302, r.status_code)
        self.assertIsNone(self.row(pid), "没判过卷的卷子应该硬删（行都没了）")
        self.assertFalse(os.path.isdir(folder), "没判过卷的产物目录应该删掉")


class TestSoftDelete(_Base):
    def setUp(self):
        self.pid = "P20261031-301"
        self.folder = add_paper(self.db_path, self.out_dir, self.pid, "判过卷的卷子",
                                submissions=2)
        self.sids = ["S20261031-301-01", "S20261031-301-02"]
        r = self.client.post("/papers/%s/delete" % self.pid, follow_redirects=False)
        self.assertEqual(302, r.status_code)

    def test_row_kept_with_deleted_at(self):
        row = self.row(self.pid)
        self.assertIsNotNone(row, "判过卷的卷子不能硬删")
        self.assertTrue(row.get("deleted_at"), "deleted_at 没写进去")

    def test_folder_and_submissions_kept(self):
        self.assertTrue(os.path.isdir(self.folder), "判卷证据目录被删了")
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "evidence.bin")))
        self.assertEqual(2, self.submissions(self.pid), "判卷记录被删了")

    def test_hidden_from_list(self):
        html = self.client.get("/papers").text
        self.assertNotIn(self.pid, html)

    def test_preview_returns_410(self):
        r = self.client.get("/papers/%s" % self.pid)
        self.assertEqual(410, r.status_code)
        t = text_of(r.text)
        self.assertIn("已删除", t)
        self.assertIn("判卷记录", t)

    def test_pdfs_return_410(self):
        for path in ("/papers/%s/pdf" % self.pid, "/papers/%s/card.pdf" % self.pid,
                     "/papers/%s/print.pdf" % self.pid):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(410, r.status_code)
                self.assertIn("已删除", text_of(r.text))

    def test_grade_result_still_reachable(self):
        r = self.client.get("/submissions/%s" % self.sids[0])
        self.assertEqual(200, r.status_code, "软删不能影响历史判卷结果的查看")


class TestListShowsSubmissionCount(_Base):
    def test_list_row_shows_count(self):
        add_paper(self.db_path, self.out_dir, "P20261031-401", "有三次判卷", submissions=3)
        add_paper(self.db_path, self.out_dir, "P20261031-402", "没判过卷", submissions=0)
        t = text_of(self.client.get("/papers").text)
        self.assertIn("判卷 3 次", t)
        self.assertIn("未判卷", t)

    def test_delete_hint_mentions_evidence(self):
        add_paper(self.db_path, self.out_dir, "P20261031-403", "判过卷", submissions=1)
        html = self.client.get("/papers").text
        self.assertIn("判卷记录", html)
        self.assertIn("保留", html)


class TestHealthEvidence(_Base):
    def test_healthz_has_evidence_mb(self):
        add_paper(self.db_path, self.out_dir, "P20261031-501", "有判卷", submissions=2,
                  payload_bytes=4096)
        p = self.client.get("/healthz").json()
        self.assertIn("papers_with_submissions_mb", p)
        self.assertIsInstance(p["papers_with_submissions_mb"], (int, float))
        self.assertGreaterEqual(p["papers_with_submissions_mb"], 0)

    def test_health_page_shows_evidence(self):
        add_paper(self.db_path, self.out_dir, "P20261031-502", "有判卷", submissions=1)
        t = text_of(self.client.get("/health").text)
        self.assertIn("判卷证据", t)


if __name__ == "__main__":
    unittest.main()
