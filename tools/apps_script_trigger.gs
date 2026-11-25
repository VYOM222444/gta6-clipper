/**
 * GTA6 Clipper: reliable scheduler (Google Apps Script, runs in the owner's Google account).
 *
 * Why: GitHub skips or delays many scheduled (cron) runs. Google's time-driven triggers are
 * reliable, so this script asks GitHub to start the workflows instead:
 *   startPipeline()   every 6 hours -> Scout (which then starts Tracker -> Signals -> Watcher)
 *   startResearcher() once a day    -> Researcher (~07:00 IST)
 *   startDecider()    once a day    -> Decider: today's batch (~18:00 IST)
 *   pollLive()        every 5 min   -> live-stream mode: counts the chat of GTA 6 streams that are
 *                     live right now (Candidates kind=live_now, found by Scout) into the LiveChat
 *                     tab; when a stream ends it marks it live_vod and starts Signals, so its chat
 *                     spikes become clips within minutes (no Colab / yt-dlp needed).
 *                     Needs Script property YT_API_KEY (the YouTube Data API key). Off if missing.
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

function startDecider() {
  dispatch_('decider.yml');
}

// --- live-stream mode ----------------------------------------------------------------------
var SHEET_ID = '1hq8aaJVeoPyUzQv01V1zJwxAMYvVIee5fx1ri33el9E';
var LIVE_MAX_STREAMS = 3;          // streams polled at once (best Scout score first)
var LIVE_MAX_PAGES = 3;            // chat pages per stream per poll (busy chats)
var LIVE_DAILY_UNITS = 2500;       // YouTube quota for live mode per day (chat list = 5 units)
var BIN = 10;                      // seconds per bin (= rules.json signals.chat_window_seconds)
// Copy of playbook/rules.json signals.reaction_words (a test checks they match).
var REACTION_WORDS = ["lmao", "lmfao", "lol", "💀", "😂", "🤣", "clip it", "clip that", "no way", "omg", "w", "wtf", "bro", "haha", "hahaha", "😭", "🔥", "bhai", "op", "gg", "insane", "crazy"];
var LIVECHAT_HEADERS = ['key', 'video_id', 'bin_s', 'reactions', 'messages', 'updated_at'];

function isReaction_(text) {
  var t = String(text || '').toLowerCase();
  for (var i = 0; i < REACTION_WORDS.length; i++) {
    var w = REACTION_WORDS[i];
    if (/^[a-z ]+$/.test(w)) {
      // last letter may repeat: "lmao" matches "lmaooo", "w" matches "www"
      var re = new RegExp('\\b' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '+\\b');
      if (re.test(t)) return true;
    } else if (t.indexOf(w) >= 0) {
      return true;
    }
  }
  return false;
}

function ytGet_(path, params, key, units) {
  var props = PropertiesService.getScriptProperties();
  var day = Utilities.formatDate(new Date(), 'America/Los_Angeles', 'yyyy-MM-dd');
  var used = Number(props.getProperty('live_units_' + day) || 0);
  if (used + units > LIVE_DAILY_UNITS) throw new Error('live mode quota for today used (' + used + ')');
  props.setProperty('live_units_' + day, String(used + units));
  var q = Object.keys(params).map(function (k) { return k + '=' + encodeURIComponent(params[k]); }).join('&');
  var res = UrlFetchApp.fetch('https://www.googleapis.com/youtube/v3/' + path + '?' + q + '&key=' + key,
                              { muteHttpExceptions: true });
  var data = JSON.parse(res.getContentText() || '{}');
  if (res.getResponseCode() >= 400) {
    var reason = (((data.error || {}).errors || [])[0] || {}).reason || '';
    var e = new Error(path + ' HTTP ' + res.getResponseCode() + ' ' + reason);
    e.reason = reason;
    throw e;  // never includes the key
  }
  return data;
}

function headerIndex_(headers) {
  var idx = {};
  headers.forEach(function (h, i) { idx[String(h).trim()] = i; });
  return idx;
}

function liveSheet_(ss) {
  var sh = ss.getSheetByName('LiveChat');
  if (!sh) {
    sh = ss.insertSheet('LiveChat');
    sh.getRange(1, 1, 1, LIVECHAT_HEADERS.length).setValues([LIVECHAT_HEADERS]);
  }
  return sh;
}

/** Add counts {bin_s: [reactions, messages]} of one video to the LiveChat tab (one row per bin). */
function writeBins_(sh, vid, bins) {
  var keys = Object.keys(bins);
  if (!keys.length) return;
  var last = sh.getLastRow();
  var existing = last > 1 ? sh.getRange(2, 1, last - 1, 5).getValues() : [];
  var rowOf = {};
  existing.forEach(function (r, i) { rowOf[r[0]] = i; });
  var now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  var add = [];
  keys.forEach(function (b) {
    var key = vid + '-' + b;
    if (key in rowOf) {
      var r = existing[rowOf[key]];
      r[3] = Number(r[3] || 0) + bins[b][0];
      r[4] = Number(r[4] || 0) + bins[b][1];
      sh.getRange(rowOf[key] + 2, 4, 1, 3).setValues([[r[3], r[4], now]]);
    } else {
      add.push([key, vid, Number(b), bins[b][0], bins[b][1], now]);
    }
  });
  if (add.length) sh.getRange(sh.getLastRow() + 1, 1, add.length, 6).setValues(add);
}

function markEnded_(cand, idx, rowNum) {
  cand.getRange(rowNum, idx.kind + 1).setValue('live_vod');
}

/** Every 5 min: count live chat of GTA 6 streams that are live now. */
function pollLive() {
  var props = PropertiesService.getScriptProperties();
  var key = props.getProperty('YT_API_KEY');
  if (!key) { console.log('Live mode off: add Script property YT_API_KEY.'); return; }
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var cand = ss.getSheetByName('Candidates');
  var values = cand.getDataRange().getValues();
  var idx = headerIndex_(values[0]);
  var rows = [];
  for (var i = 1; i < values.length; i++) {
    var r = values[i];
    if (r[idx.kind] === 'live_now' && String(r[idx.status]).indexOf('blocked') !== 0) {
      rows.push({ vid: String(r[idx.video_id]), score: Number(r[idx.score] || 0), rowNum: i + 1,
                  title: String(r[idx.title] || '') });
    }
  }
  rows.sort(function (a, b) { return b.score - a.score; });
  rows = rows.slice(0, LIVE_MAX_STREAMS);
  if (!rows.length) { console.log('No GTA 6 stream live right now (Candidates kind=live_now).'); return; }
  var sh = liveSheet_(ss), ended = 0;
  rows.forEach(function (s) {
    var st = JSON.parse(props.getProperty('live_' + s.vid) || '{}');
    try {
      if (!st.chat) {
        var v = ytGet_('videos', { part: 'liveStreamingDetails', id: s.vid }, key, 1).items || [];
        var d = (v[0] || {}).liveStreamingDetails || {};
        if (!d.activeLiveChatId || d.actualEndTime) {
          markEnded_(cand, idx, s.rowNum); ended++;
          console.log(s.vid + ': not live (or no public chat); marked live_vod.');
          props.deleteProperty('live_' + s.vid);
          return;
        }
        st = { chat: d.activeLiveChatId, start: d.actualStartTime, token: '' };
      }
      var start = new Date(st.start).getTime(), bins = {}, n = 0, react = 0, done = false;
      for (var p = 0; p < LIVE_MAX_PAGES; p++) {
        var params = { liveChatId: st.chat, part: 'snippet', maxResults: 2000 };
        if (st.token) params.pageToken = st.token;
        var data;
        try {
          data = ytGet_('liveChat/messages', params, key, 5);
        } catch (e) {
          if (e.reason === 'liveChatEnded' || e.reason === 'liveChatNotFound' || e.reason === 'forbidden') {
            done = true; break;
          }
          throw e;
        }
        (data.items || []).forEach(function (it) {
          var sn = it.snippet || {};
          var t = (new Date(sn.publishedAt).getTime() - start) / 1000;
          if (!(t >= 0)) return;
          var b = Math.floor(t / BIN) * BIN;
          bins[b] = bins[b] || [0, 0];
          bins[b][1]++; n++;
          if (isReaction_(sn.displayMessage)) { bins[b][0]++; react++; }
        });
        st.token = data.nextPageToken || st.token;
        if (data.offlineAt) { done = true; break; }
        if ((data.items || []).length < 2000) break;
      }
      writeBins_(sh, s.vid, bins);
      console.log(s.vid + ': ' + n + ' messages, ' + react + ' reactions | ' + s.title.slice(0, 50));
      if (done) {
        markEnded_(cand, idx, s.rowNum); ended++;
        props.deleteProperty('live_' + s.vid);
        console.log(s.vid + ': stream ended -> live_vod.');
      } else {
        props.setProperty('live_' + s.vid, JSON.stringify(st));
      }
    } catch (e) {
      console.log(s.vid + ': ' + e.message);
    }
  });
  if (ended && props.getProperty('GH_TOKEN')) {
    dispatch_('signals.yml');  // chat spikes of the ended stream -> Watcher -> Gatekeeper
  }
}

/** Run this once by hand: creates both timers (deletes old ones first, so it is safe to re-run). */
function installTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('startPipeline').timeBased().everyHours(6).create();
  ScriptApp.newTrigger('startResearcher').timeBased().everyDays(1).atHour(7)
    .inTimezone('Asia/Kolkata').create();
  ScriptApp.newTrigger('startDecider').timeBased().everyDays(1).atHour(18)
    .inTimezone('Asia/Kolkata').create();
  ScriptApp.newTrigger('pollLive').timeBased().everyMinutes(5).create();
  console.log('Triggers installed: startPipeline every 6 h, startResearcher ~07:00 IST, ' +
              'startDecider ~18:00 IST, pollLive every 5 min.');
}
