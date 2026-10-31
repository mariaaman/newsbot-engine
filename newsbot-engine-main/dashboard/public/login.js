// Log-in page: password (+ the 6-digit code when two-step is on). No inline script: the page's CSP allows only files from this site.
(function () {
  var form = document.getElementById("login-form"), pw = document.getElementById("pw"), code = document.getElementById("code");
  var codeRow = document.getElementById("code-row"), msg = document.getElementById("msg"), go = document.getElementById("go");

  function say(text) { msg.textContent = text; msg.hidden = !text; }

  fetch("/api/session", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (s) { if (s && s.authenticated) { location.href = "/"; return; } codeRow.hidden = !(s && s.totp); })
    .catch(function () { /* the form still works */ });

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    say("");
    go.disabled = true;
    var body = { password: pw.value };
    if (!codeRow.hidden) body.code = code.value.trim();
    fetch("/api/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "newsbot" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j }; });
    }).then(function (x) {
      if (x.status === 200) { pw.value = ""; location.href = "/"; return; }
      if (x.status === 429) say("অনেকবার ভুল হয়েছে। ১৫ মিনিট পরে আবার চেষ্টা করুন।");
      else if (x.status === 401) say(codeRow.hidden ? "পাসওয়ার্ড ভুল।" : "পাসওয়ার্ড বা কোড ভুল।");
      else say("লগইন করা যায়নি (" + (x.j.error || x.status) + ")।");
      pw.value = ""; code.value = "";
    }).catch(function () { say("সংযোগ হয়নি। ইন্টারনেট দেখে আবার চেষ্টা করুন।"); })
      .then(function () { go.disabled = false; });
  });
})();
