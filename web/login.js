'use strict';
const form = document.getElementById('login-form');
const errorBox = document.getElementById('login-error');
const submit = document.getElementById('login-submit');

// Endast lokala sökvägar tillåts som mål efter inloggning.
/** @returns {string} */
function safeNext() {
  const next = new URLSearchParams(window.location.search).get('next') || '/';
  if (!next.startsWith('/') || next.startsWith('//') || next.includes('\\') || next.startsWith('/login')) return '/';
  return next;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorBox.textContent = '';
  submit.disabled = true;
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('login-username').value,
        password: document.getElementById('login-password').value
      })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    window.location.replace(safeNext());
  } catch (error) {
    errorBox.textContent = error.message;
    document.getElementById('login-password').value = '';
    document.getElementById('login-password').focus();
    submit.disabled = false;
  }
});
