const printButton = document.querySelector('[data-print]');
async function prepareReceipt() {
  await Promise.all(Array.from(document.querySelectorAll('.receipt img'), image => {
    if (image.complete) return Promise.resolve();
    return new Promise(resolve => {
      image.addEventListener('load', resolve, {once: true});
      image.addEventListener('error', resolve, {once: true});
    });
  }));
  if (document.fonts?.ready) await document.fonts.ready;
}
printButton?.addEventListener('click', async () => {
  printButton.disabled = true;
  try {
    await prepareReceipt();
    const failedLogo = document.querySelector('.receipt-logo');
    if (failedLogo && !failedLogo.naturalWidth) {
      window.alert('The organization logo could not load. Reload this receipt before printing.');
      return;
    }
    window.print();
  } finally {
    printButton.disabled = false;
  }
});
