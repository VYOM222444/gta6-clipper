/**
 * GTA6 Clipper: reliable scheduler (Google Apps Script, runs in the owner's Google account).
 *
 * Why: GitHub skips or delays many scheduled (cron) runs. Google's time-driven triggers are
 * reliable, so this script asks GitHub to start the workflows instead:
 *   startPipeline()   every 6 hours -> Scout (which then starts Tracker -> Signals -> Watcher)
 *   startResearcher() once a day    -> Researcher
 * Each workflow has a guard job: if it really ran recently it skips, so overlapping with
 * GitHub's own (backup) schedule is harmless.
 *
 * Setup (once, on the laptop): see the "Reliable scheduler" steps in CLAUDE.md section 10.
 * The GitHub token lives ONLY in this script's Script Properties (key GH_TOKEN): a
 * fine-grained token for this one repository with "Actions: Read and write" and nothing else.
 */
var REPO = 'VYOM222444/gta6-clipper';

function dispatch_(workflowFile) {
  var token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (!token) {
    throw new Error('Script property GH_TOKEN is missing (Project Settings > Script properties).');
  }
  var res = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/actions/workflows/' + workflowFile + '/dispatches',
    {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      payload: JSON.stringify({ ref: 'main', inputs: { from_trigger: 'apps-script' } }),
      muteHttpExceptions: true
    }
  );
  var code = res.getResponseCode();
  // 204 = started. Never log the token; only the status code and GitHub's message.
  console.log(workflowFile + ': HTTP ' + code + (code === 204 ? ' (started)' : ' ' + res.getContentText().slice(0, 200)));
  if (code !== 204) {
    throw new Error(workflowFile + ' could not be started (HTTP ' + code + ')');
  }
}

function startPipeline() {
  dispatch_('scout.yml');
}

function startResearcher() {
  dispatch_('researcher.yml');
}

/** Run this once by hand: creates both timers (deletes old ones first, so it is safe to re-run). */
function installTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('startPipeline').timeBased().everyHours(6).create();
  ScriptApp.newTrigger('startResearcher').timeBased().everyDays(1).atHour(7)
    .inTimezone('Asia/Kolkata').create();
  console.log('Triggers installed: startPipeline every 6 h, startResearcher daily ~07:00 IST.');
}
