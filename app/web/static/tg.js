/* Панель, открытая кнопкой бота «Открыть CRM» (Telegram Mini App):
   сказать Telegram, что страница готова, и развернуть её на весь экран -
   иначе на телефоне она открывается в половину высоты. Вне Telegram моста
   нет, и скрипт ничего не делает. Официальный telegram-web-app.js не
   грузим: панель не тянет чужих скриптов, а из него нужны два события. */
(function () {
  var proxy = window.TelegramWebviewProxy;
  if (!proxy || typeof proxy.postEvent !== 'function') return;
  try {
    proxy.postEvent('web_app_ready');
    proxy.postEvent('web_app_expand');
  } catch (e) { /* старый клиент без событий: страница и так открыта */ }
})();
