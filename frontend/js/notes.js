/**
 * notes.js — What you were doing, written down beside what you did (#530).
 *
 * A change window produces a running commentary: "16:02 shut Gi1/0/24,
 * 16:05 confirmed by site, 16:40 rolled back". It lives in Notepad, it is
 * never searched again, and it never meets the transcript it describes.
 *
 * Here it belongs to the session, so it is kept with the commands it is
 * about, survives a restart, and is found by searching for what somebody
 * wrote rather than for what a device printed.
 *
 * Three decisions worth stating, because each is a way this could annoy
 * somebody at exactly the wrong moment:
 *
 * **It saves on its own, on a delay.** Nobody types a commentary and then
 * remembers to press Save; a note that needs saving is a note that gets
 * lost when the window closes. But a write per keystroke is a write per
 * keystroke, so it settles for a second first — and flushes immediately on
 * close, because closing is a moment somebody expects to be final.
 *
 * **A session with no history record is refused, and says so.** The
 * backend will not file a note against an id it has never seen: the note
 * would be unfindable and "saved" would be a lie nobody could check.
 * Recording can be switched off, so this state is reachable.
 *
 * **It is never sent to the assistant.** Notes carry things people write
 * for themselves — a customer's name, why a change was really made, what
 * somebody said on the phone. The chat context is built from terminal
 * buffers and nothing here reaches it; pasting is the only route, and
 * that is a deliberate act.
 */

(function () {
  'use strict';

  const SAVE_AFTER_MS = 1000;

  let overlay, textarea, stateLine, forLine;
  /** The session the open drawer belongs to. */
  let sessionId = '';
  let timer = null;
  /** What was last written, so a flush with nothing new does nothing. */
  let lastSaved = '';

  document.addEventListener('DOMContentLoaded', () => {
    overlay = document.getElementById('notes-overlay');
    if (!overlay) return;
    textarea = document.getElementById('notes-text');
    stateLine = document.getElementById('notes-state');
    forLine = document.getElementById('notes-for');

    document.getElementById('notes-close')
      .addEventListener('click', closeNotes);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeNotes();
    });
    document.getElementById('notes-stamp')
      .addEventListener('click', insertTimestamp);

    textarea.addEventListener('input', () => {
      _say('unsaved changes');
      clearTimeout(timer);
      timer = setTimeout(save, SAVE_AFTER_MS);
    });

    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        if (overlay.classList.contains('hidden')) openNotes();
        else closeNotes();
      }
      // Escape closes it, but only when the focus is inside — a note being
      // typed over a terminal must not vanish because somebody pressed
      // Escape at the device.
      if (e.key === 'Escape' && !overlay.classList.contains('hidden')
          && overlay.contains(document.activeElement)) {
        closeNotes();
      }
    });
  });

  function _activeTab() {
    if (typeof window.getActiveTab === 'function') return window.getActiveTab();
    return null;
  }

  function _say(text, kind) {
    if (!stateLine) return;
    stateLine.textContent = text || '';
    stateLine.className = kind === 'bad' ? 'field-hint ansible-note-bad'
                                         : 'field-hint';
  }

  /**
   * Open the drawer on the active tab's session.
   *
   * The tab is read at open time rather than followed: a note being typed
   * about one device must not start writing itself into another because
   * somebody clicked a different tab to look something up.
   */
  async function openNotes() {
    const tab = _activeTab();
    if (!tab || !tab.sessionId) {
      if (window.shellmateDialog) {
        window.shellmateDialog.alert({
          title: 'Notes',
          body: 'Notes belong to a session. Open a connection first.',
        });
      }
      return;
    }

    sessionId = tab.sessionId;
    forLine.textContent = tab.label || tab.hostname || '';
    textarea.value = '';
    lastSaved = '';
    _say('loading…');
    overlay.classList.remove('hidden');
    textarea.focus();

    try {
      const data = await (await fetch(
        `/api/history/sessions/${encodeURIComponent(sessionId)}/notes`)).json();
      textarea.value = data.notes || '';
      lastSaved = textarea.value;
      _say(textarea.value ? 'saved' : '');
    } catch (e) {
      _say('could not be read', 'bad');
    }
    // The cursor at the end, where the next line goes. A commentary is
    // appended to, not edited from the top.
    textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
  }

  function closeNotes() {
    clearTimeout(timer);
    // Flushed rather than left to the timer: closing is a moment somebody
    // expects to be final, and a second of unsaved text is exactly the
    // second the window gets shut in.
    save().finally(() => overlay.classList.add('hidden'));
  }

  async function save() {
    if (!sessionId) return;
    const text = textarea.value;
    if (text === lastSaved) return;
    _say('saving…');
    try {
      const response = await fetch(
        `/api/history/sessions/${encodeURIComponent(sessionId)}/notes`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ notes: text }),
        });
      if (response.status === 404) {
        // Reachable: history recording can be switched off, and then there
        // is no session row to hang a note on. Said plainly, because the
        // alternative is somebody typing a commentary into a box that
        // silently keeps none of it.
        _say('this session is not being recorded, so notes cannot be kept',
             'bad');
        return;
      }
      if (!response.ok) throw new Error(`the server answered ${response.status}`);
      lastSaved = text;
      _say('saved');
    } catch (e) {
      _say(`not saved: ${e.message || e}`, 'bad');
    }
  }

  /**
   * The time, at the start of a fresh line.
   *
   * The whole point of a running commentary is when things happened, and
   * typing "16:02" by hand while watching a device reload is how the times
   * end up approximate.
   */
  function insertTimestamp() {
    const now = new Date();
    const stamp = `${String(now.getHours()).padStart(2, '0')}:`
                + `${String(now.getMinutes()).padStart(2, '0')} `;
    const at = textarea.selectionStart;
    const before = textarea.value.slice(0, at);
    const after = textarea.value.slice(textarea.selectionEnd);
    const lead = (!before || before.endsWith('\n')) ? '' : '\n';
    textarea.value = `${before}${lead}${stamp}${after}`;
    const cursor = before.length + lead.length + stamp.length;
    textarea.selectionStart = textarea.selectionEnd = cursor;
    textarea.focus();
    _say('unsaved changes');
    clearTimeout(timer);
    timer = setTimeout(save, SAVE_AFTER_MS);
  }

  window.openNotes = openNotes;
})();
