/* 数学题库工作台 · 原生 JS（无框架、无构建）。只做几件小事，别让它长胖。 */
(function () {
  "use strict";

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  var form = $("#paper-form");

  /* 1) 题量配比 → 实时「合计 N 题」 */
  var totalEl = $("#total-count");
  var mixInputs = $$("input[data-qtype]");

  function refreshTotal() {
    if (!totalEl) { return; }
    var sum = 0;
    mixInputs.forEach(function (input) {
      var value = parseInt(input.value, 10);
      if (!isNaN(value) && value > 0) { sum += value; }
    });
    totalEl.textContent = String(sum);
  }

  mixInputs.forEach(function (input) {
    input.addEventListener("input", refreshTotal);
    input.addEventListener("change", refreshTotal);
  });
  refreshTotal();

  /* 2) 勾章 = 勾上该章全部知识点（章节与知识点会一起提交，章节非空时以章节为准） */
  $$("input[data-chapter-toggle]").forEach(function (chapterBox) {
    var index = chapterBox.getAttribute("data-chapter-toggle");
    var kids = $$('input[data-chapter="' + index + '"]');

    chapterBox.addEventListener("change", function () {
      kids.forEach(function (kid) { kid.checked = chapterBox.checked; });
    });

    kids.forEach(function (kid) {
      kid.addEventListener("change", function () {
        var checked = kids.filter(function (k) { return k.checked; }).length;
        chapterBox.checked = kids.length > 0 && checked === kids.length;
        chapterBox.indeterminate = checked > 0 && checked < kids.length;
      });
    });
  });

  /* 3) 「刷新可出题量」：t80 起这段整页跳转已彻底删除。
        原因：旧代码把表单参数拼成查询串整页重载，却不带 subject_pk，
        重载后学科被重置（用户看到的「一点就跳回数学」）。
        现在刷新由 templates/index.html 里的就地 fetch(/api/available) 负责，
        本文件不再做任何整页跳转（回归断言见 tests/test_compose_buttons.py）。 */

  /* 4) 建卷要等十几秒：提交后按钮置灰 + 拉起遮罩；返回/错误页时遮罩必须能消失 */
  var overlay = $("#compose-overlay");

  function showOverlay() {
    if (!overlay) { return; }
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
  }

  function hideOverlay() {
    if (overlay) {
      overlay.hidden = true;
      overlay.setAttribute("aria-hidden", "true");
    }
    var button = $("#compose-submit");
    if (button) {
      button.disabled = false;
      button.textContent = "生成试卷";
    }
  }

  if (form) {
    form.addEventListener("submit", function () {
      var button = $("#compose-submit");
      var note = $("#form-note");
      showOverlay();
      if (button) {
        button.disabled = true;
        button.textContent = "正在生成，请稍等…";
      }
      if (note) {
        note.textContent = "正在排版卷面并生成答题卡，大约十几秒，别关这个页面。";
      }
    });
  }

  /* 浏览器「后退」会从缓存恢复页面：把遮罩和按钮状态收回去，否则看起来像卡死 */
  window.addEventListener("pageshow", function () { hideOverlay(); });
  window.addEventListener("beforeunload", function () { hideOverlay(); });
})();
