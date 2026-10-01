// The server remains authoritative for cooldown and hourly limits.
const resend = document.getElementById('resend-code');
const countdown = document.getElementById('resend-countdown');
if (resend && countdown) {
  const readyAt = Date.now() + Number(resend.dataset.wait || 0) * 1000;
  const timer = setInterval(() => {
    const seconds = Math.max(0, Math.ceil((readyAt - Date.now()) / 1000));
    countdown.textContent = seconds ? `Request another code in ${seconds} seconds.` : 'You can request another code now.';
    if (!seconds) {
      resend.disabled = false;
      clearInterval(timer);
    }
  }, 1000);
}
