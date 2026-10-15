#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>
#include <stdio.h>
#include <string.h>

#include "ui_approve.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_RED		lv_color_hex(0xE74C3C)
#define COL_GREEN	lv_color_hex(0x2ECC71)
#define COL_AMBER	lv_color_hex(0xF1C40F)
#define COL_PANEL	lv_color_hex(0x161A20)

#define ID_MAX		12
#define CARD_W		304
#define CARD_H		216
#define CMD_BOX_H	108

/*
 * Taps are ignored for this long after the card appears. A person who is
 * tapping the panel to silence the blinking frame (usage_view.c) lands that
 * tap on whatever is under their finger, and for a few hundred milliseconds
 * that would be Allow. The buttons are dimmed until they arm, so the delay
 * reads as "not yet" instead of as a dead panel.
 */
#define ARM_AFTER_MS	700

static lv_obj_t *card;
static lv_obj_t *left_lbl;
static lv_obj_t *btn_allow;
static lv_obj_t *btn_deny;
static lv_timer_t *tick;
static char cur_id[ID_MAX];
static int64_t shown_ms;
static int64_t expires_ms;
static bool armed;

static char ans_id[ID_MAX];
static int ans_res;
static bool ans_pending;

static void queue_answer(const char *id, int res)
{
	snprintf(ans_id, sizeof(ans_id), "%s", id);
	ans_res = res;
	ans_pending = true;
}

static void close_card(void)
{
	if (tick) {
		lv_timer_delete(tick);
		tick = NULL;
	}
	if (card) {
		lv_obj_delete(card);
		card = NULL;
	}
	left_lbl = NULL;
	btn_allow = NULL;
	btn_deny = NULL;
	cur_id[0] = '\0';
}

static void answer_cb(lv_event_t *e)
{
	int res = (int)(intptr_t)lv_event_get_user_data(e);

	if (!card || !armed) {
		return;
	}
	printk("[approve] %s %s\n", res == UI_APPROVE_ALLOW ? "allowed" : "denied",
	       cur_id);
	queue_answer(cur_id, res);
	close_card();
}

static void tick_cb(lv_timer_t *t)
{
	ARG_UNUSED(t);
	if (!card) {
		return;
	}
	int64_t now = k_uptime_get();

	if (!armed && now - shown_ms >= ARM_AFTER_MS) {
		armed = true;
		lv_obj_set_style_opa(btn_allow, LV_OPA_COVER, 0);
		lv_obj_set_style_opa(btn_deny, LV_OPA_COVER, 0);
	}
	if (now >= expires_ms) {
		/* Nothing is sent: the daemon's own deadline lapses with ours
		 * and the terminal's prompt is what is left. */
		printk("[approve] expired %s\n", cur_id);
		close_card();
		return;
	}
	if (left_lbl) {
		lv_label_set_text_fmt(left_lbl, "or answer at the computer  %ds",
				      (int)((expires_ms - now + 999) / 1000));
	}
}

static lv_obj_t *mk_button(lv_obj_t *parent, const char *txt, lv_color_t bg,
			   lv_color_t ink, int dx, int answer)
{
	lv_obj_t *b = lv_btn_create(parent);

	lv_obj_set_size(b, 136, 44);
	lv_obj_align(b, LV_ALIGN_BOTTOM_MID, dx, -8);
	lv_obj_set_style_bg_color(b, bg, 0);
	lv_obj_set_style_shadow_width(b, 0, 0);
	/* Born dimmed; tick_cb brightens both once they arm. */
	lv_obj_set_style_opa(b, LV_OPA_40, 0);
	/* A control's gesture stops at the control, as in ui_settings.c: a tap
	 * that drifts must not become a swipe that moves the screen away from
	 * a question somebody is answering. */
	lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(b, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(b, answer_cb, LV_EVENT_CLICKED,
			    (void *)(intptr_t)answer);

	lv_obj_t *l = lv_label_create(b);

	lv_label_set_text(l, txt);
	lv_obj_set_style_text_color(l, ink, 0);
	lv_obj_center(l);
	return b;
}

bool ui_approve_show(const char *id, const char *tool, const char *cmd,
		     int ttl_s)
{
	if (id == NULL || id[0] == '\0') {
		return false;
	}
	if (card) {
		/* The same request again (the daemon re-sends after a
		 * reconnect) is not a second one. */
		return strcmp(id, cur_id) == 0;
	}
	if (ttl_s < 5) {
		ttl_s = 5;
	}
	if (ttl_s > 120) {
		ttl_s = 120;
	}
	snprintf(cur_id, sizeof(cur_id), "%s", id);
	shown_ms = k_uptime_get();
	expires_ms = shown_ms + (int64_t)ttl_s * 1000;
	armed = false;

	card = lv_obj_create(lv_layer_top());
	lv_obj_set_size(card, CARD_W, CARD_H);
	lv_obj_center(card);
	lv_obj_set_style_bg_color(card, COL_BG, 0);
	lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(card, COL_AMBER, 0);
	lv_obj_set_style_border_width(card, 2, 0);
	lv_obj_set_style_radius(card, 10, 0);
	lv_obj_set_style_pad_all(card, 0, 0);
	lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
	/* Swallow gestures so a swipe cannot slide the dashboard away from
	 * under an open question. */
	lv_obj_clear_flag(card, LV_OBJ_FLAG_GESTURE_BUBBLE);

	lv_obj_t *title = lv_label_create(card);

	lv_label_set_long_mode(title, LV_LABEL_LONG_DOT);
	lv_obj_set_width(title, CARD_W - 24);
	lv_label_set_text_fmt(title, "Allow %s?", tool && tool[0] ? tool : "this");
	lv_obj_set_style_text_color(title, COL_AMBER, 0);
	lv_obj_align(title, LV_ALIGN_TOP_LEFT, 12, 8);

	/* The text in a clipped box of its own. The daemon only sends what fits
	 * (180 characters, six lines at this width), so clipping is a guard and
	 * not a design: if it ever did clip, the owner would see a command that
	 * ends mid-box rather than one that quietly scrolled. */
	lv_obj_t *box = lv_obj_create(card);

	lv_obj_set_size(box, CARD_W - 24, CMD_BOX_H);
	lv_obj_align(box, LV_ALIGN_TOP_LEFT, 12, 30);
	lv_obj_set_style_bg_color(box, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(box, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(box, 0, 0);
	lv_obj_set_style_radius(box, 6, 0);
	lv_obj_set_style_pad_all(box, 6, 0);
	lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(box, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_add_flag(box, LV_OBJ_FLAG_GESTURE_BUBBLE);

	lv_obj_t *body = lv_label_create(box);

	lv_label_set_long_mode(body, LV_LABEL_LONG_WRAP);
	lv_obj_set_width(body, CARD_W - 24 - 12);
	lv_label_set_text(body, cmd ? cmd : "");
	lv_obj_set_style_text_color(body, COL_TEXT, 0);
	lv_obj_align(body, LV_ALIGN_TOP_LEFT, 0, 0);

	left_lbl = lv_label_create(card);
	lv_obj_set_style_text_color(left_lbl, COL_DIM, 0);
	lv_label_set_text(left_lbl, "");
	lv_obj_align(left_lbl, LV_ALIGN_TOP_LEFT, 12, 142);

	btn_deny = mk_button(card, "Deny", COL_TRACK, COL_RED, -72,
			     UI_APPROVE_DENY);
	btn_allow = mk_button(card, "Allow", COL_GREEN, COL_BG, 72,
			      UI_APPROVE_ALLOW);

	tick = lv_timer_create(tick_cb, 250, NULL);
	tick_cb(NULL);
	printk("[approve] shown %s\n", cur_id);
	return true;
}

void ui_approve_end(const char *id)
{
	if (card && id && strcmp(id, cur_id) == 0) {
		printk("[approve] withdrawn %s\n", cur_id);
		close_card();
	}
}

bool ui_approve_active(void)
{
	return card != NULL;
}

bool ui_approve_take_answer(char *id, size_t idlen, int *res)
{
	if (!ans_pending || id == NULL || res == NULL || idlen == 0) {
		return false;
	}
	snprintf(id, idlen, "%s", ans_id);
	*res = ans_res;
	ans_pending = false;
	return true;
}

void ui_approve_decline(const char *id)
{
	queue_answer(id, UI_APPROVE_DECLINE);
}
