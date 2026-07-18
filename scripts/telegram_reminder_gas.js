/**
 * Google Apps Script for Telegram learning reminder pings.
 *
 * Script Properties:
 * - APP_URL: https://japanese-learning-kasuga72.onrender.com
 * - TELEGRAM_REMINDER_SECRET: same value as the Render environment variable
 */

function pingTelegramReminder() {
  const props = PropertiesService.getScriptProperties();
  const appUrl = (props.getProperty('APP_URL') || 'https://japanese-learning-kasuga72.onrender.com').replace(/\/$/, '');
  const secret = props.getProperty('TELEGRAM_REMINDER_SECRET') || '';
  const endpoint = appUrl + '/api/telegram/reminder-run' + (secret ? '?secret=' + encodeURIComponent(secret) : '');

  const response = UrlFetchApp.fetch(endpoint, {
    method: 'post',
    muteHttpExceptions: true,
    followRedirects: true,
  });

  Logger.log('[telegram-reminder] status=' + response.getResponseCode() + ' body=' + response.getContentText());
}

function createReminderTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (trigger) {
      return trigger.getHandlerFunction() === 'pingTelegramReminder';
    })
    .forEach(function (trigger) {
      ScriptApp.deleteTrigger(trigger);
    });

  ScriptApp.newTrigger('pingTelegramReminder')
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log('[telegram-reminder] created every 5 minutes trigger');
}
