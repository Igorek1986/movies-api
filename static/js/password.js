/**
 * initPasswordValidation — live password strength & match UI
 *
 * opts:
 *   passwordId        — id of password input
 *   confirmId         — id of confirm input
 *   strengthId        — id of strength text div
 *   matchId           — id of match text div
 *   reqIds            — { length, uppercase, lowercase, digit }
 *   submitId          — id of submit button (disabled until valid)
 *   currentPasswordId — (optional) id of current-password input
 */
function initPasswordValidation(opts) {
  const pwdEl     = document.getElementById(opts.passwordId);
  const confirmEl = document.getElementById(opts.confirmId);
  const strengthEl = document.getElementById(opts.strengthId);
  const matchEl   = document.getElementById(opts.matchId);
  const submitEl  = document.getElementById(opts.submitId);
  const currentEl = opts.currentPasswordId
    ? document.getElementById(opts.currentPasswordId) : null;

  const rules = {
    length:    { el: document.getElementById(opts.reqIds.length),    test: p => p.length >= 8 },
    uppercase: { el: document.getElementById(opts.reqIds.uppercase), test: p => /[A-Z]/.test(p) },
    lowercase: { el: document.getElementById(opts.reqIds.lowercase), test: p => /[a-z]/.test(p) },
    digit:     { el: document.getElementById(opts.reqIds.digit),     test: p => /[0-9]/.test(p) },
  };

  function checkStrength(pwd) {
    const met = Object.values(rules).filter(r => r.test(pwd)).length;
    for (const r of Object.values(rules)) {
      r.el.classList.toggle('req-met', r.test(pwd));
    }
    if (!pwd) { strengthEl.textContent = ''; return false; }
    if (met <= 2) {
      strengthEl.textContent = 'Слабый пароль';
      strengthEl.className = 'pwd-feedback pwd-weak';
    } else if (met === 3) {
      strengthEl.textContent = 'Средний пароль';
      strengthEl.className = 'pwd-feedback pwd-medium';
    } else {
      strengthEl.textContent = 'Надёжный пароль';
      strengthEl.className = 'pwd-feedback pwd-strong';
    }
    return met === 4;
  }

  function checkMatch(pwd, confirm) {
    if (!confirm) { matchEl.textContent = ''; return false; }
    const ok = pwd === confirm;
    matchEl.textContent = ok ? 'Пароли совпадают' : 'Пароли не совпадают';
    matchEl.className = 'pwd-feedback ' + (ok ? 'pwd-strong' : 'pwd-weak');
    return ok;
  }

  function update() {
    const pwd     = pwdEl.value;
    const confirm = confirmEl.value;
    const strong  = checkStrength(pwd);
    const match   = checkMatch(pwd, confirm);
    const hasCurrent = !currentEl || currentEl.value.length > 0;
    submitEl.disabled = !(strong && match && hasCurrent);
  }

  pwdEl.addEventListener('input', update);
  confirmEl.addEventListener('input', update);
  if (currentEl) currentEl.addEventListener('input', update);
}
