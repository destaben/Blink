#ifndef UI_APPROVE_H
#define UI_APPROVE_H

#include <stdbool.h>
#include <stddef.h>

/*
 * The "Allow / Deny" card for a permission request raised on the computer.
 *
 * Claude Code asks "may I run this?" at the keyboard. With the opt-in hook
 * installed (pc/approvals.py) the daemon also sends the question here, and a
 * tap on the board answers it. The terminal's own prompt is never taken away:
 * a request that is not answered here in time, or that this card cannot show
 * completely, is simply asked at the computer as it always was.
 *
 * Everything here runs on the main loop's thread -- proto.c dispatches from
 * proto_service() and lv_timer_handler() runs in the same loop -- so there is
 * no locking, and no function below may be called from the net thread.
 *
 * NOTHING ABOUT THE REQUEST IS EVER PRINTED. The text is a shell command or a
 * path, it can carry a token typed on a command line, and the console this
 * board prints to is copied into the daemon's log file. Only the 8-character
 * id appears in a printk.
 */

/* Answers. 2 is "I cannot decide this one here", which is how the board says
 * no without saying no: the daemon withdraws the request and the terminal's
 * own prompt handles it. */
#define UI_APPROVE_DENY		0
#define UI_APPROVE_ALLOW	1
#define UI_APPROVE_DECLINE	2

/*
 * Put the card up. `cmd` must be the COMPLETE text: the caller has already
 * refused a value that did not fit (msg_get_str_esc's `truncated`), because
 * approving a command you were shown half of is the failure this whole card
 * exists to avoid.
 *
 * Returns false when the card is already showing a different request. One at
 * a time: two sessions asking at once is rare, and the caller answers the
 * second with UI_APPROVE_DECLINE so it is asked at the computer instead of
 * queued behind a card nobody is watching.
 */
bool ui_approve_show(const char *id, const char *tool, const char *cmd,
		     int ttl_s);

/* The daemon withdrew `id` -- answered at the computer, or given up on. Takes
 * the card down without answering. A different id is ignored. */
void ui_approve_end(const char *id);

/* Is the card on screen? */
bool ui_approve_active(void);

/* An answer waiting to be sent, once. Copies the id into `id`, sets *res to
 * one of UI_APPROVE_*, and clears it. proto_service() calls this and does the
 * writing, so the button callback never touches the wire. */
bool ui_approve_take_answer(char *id, size_t idlen, int *res);

/* Queue UI_APPROVE_DECLINE for `id` without showing anything. */
void ui_approve_decline(const char *id);

#endif /* UI_APPROVE_H */
