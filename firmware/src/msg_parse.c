#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "msg_parse.h"

/*
 * Find `"key"` used AS A KEY, and return the pointer just after its colon.
 *
 * The "as a key" part is the whole of it. This used to take the first
 * occurrence of `"key"` anywhere in the document and then scan forward for
 * the next colon -- which reads a VALUE as a key whenever some earlier value
 * happens to spell the same word, and then attaches a completely different
 * field's colon to it.
 *
 * That is not hypothetical. `{"t":"edition","v":2,"edition":"codex"}` --
 * a message whose type has the same name as one of its fields, which is the
 * natural way to name such a message -- resolved to the literal string
 * "edition", because the match landed on the value of "t" and the next colon
 * belonged to "v". The board reported `unknown edition 'edition'` and the
 * provisioning step silently did nothing.
 *
 * So: a match only counts when the very next non-space character is the
 * colon. Anything else and the search continues past it. Every existing
 * message happens to be safe today (no type shares a name with a key of the
 * same message), which is exactly why this was never noticed.
 */
static const char *after_colon(const char *json, const char *key)
{
	char pat[48];
	snprintf(pat, sizeof(pat), "\"%s\"", key);
	size_t plen = strlen(pat);

	for (const char *k = strstr(json, pat); k != NULL;
	     k = strstr(k + 1, pat)) {
		const char *p = k + plen;

		while (*p == ' ' || *p == '\t') {
			p++;
		}
		if (*p == ':') {
			return p + 1;
		}
	}
	return NULL;
}

bool msg_get_str(const char *json, const char *key, char *buf, size_t buflen)
{
	const char *p = after_colon(json, key);
	if (p == NULL) {
		return false;
	}
	const char *q1 = strchr(p, '"');
	if (q1 == NULL) {
		return false;
	}
	const char *q2 = strchr(q1 + 1, '"');
	if (q2 == NULL) {
		return false;
	}
	size_t n = (size_t)(q2 - (q1 + 1));
	if (n >= buflen) {
		n = buflen - 1;
	}
	memcpy(buf, q1 + 1, n);
	buf[n] = '\0';
	return true;
}

static int hex4(const char *p)
{
	int v = 0;

	for (int i = 0; i < 4; i++) {
		char c = p[i];

		v <<= 4;
		if (c >= '0' && c <= '9') {
			v |= c - '0';
		} else if (c >= 'a' && c <= 'f') {
			v |= c - 'a' + 10;
		} else if (c >= 'A' && c <= 'F') {
			v |= c - 'A' + 10;
		} else {
			return -1;
		}
	}
	return v;
}

bool msg_get_str_esc(const char *json, const char *key, char *buf,
		     size_t buflen, bool *truncated)
{
	if (truncated != NULL) {
		*truncated = false;
	}
	if (buf == NULL || buflen == 0) {
		return false;
	}
	buf[0] = '\0';

	const char *p = after_colon(json, key);

	if (p == NULL) {
		return false;
	}
	while (*p == ' ' || *p == '\t') {
		p++;
	}
	if (*p != '"') {
		return false;
	}
	p++;

	size_t n = 0;
	bool cut = false;

	for (;;) {
		char out;

		if (*p == '\0') {
			buf[0] = '\0';
			return false;		/* never terminated */
		}
		if (*p == '"') {
			break;
		}
		if (*p == '\\') {
			p++;
			switch (*p) {
			case '"':
			case '\\':
			case '/':
				out = *p;
				break;
			case 'n':
				out = '\n';
				break;
			case 't':
			case 'r':
			case 'b':
			case 'f':
				out = ' ';
				break;
			case 'u': {
				int cp = hex4(p + 1);

				if (cp < 0) {
					buf[0] = '\0';
					return false;
				}
				out = (cp >= 0x20 && cp <= 0x7e) ?
					(char)cp : '?';
				p += 4;
				break;
			}
			default:
				buf[0] = '\0';
				return false;	/* not a JSON escape */
			}
		} else {
			unsigned char c = (unsigned char)*p;

			out = (c >= 0x20 && c <= 0x7e) ? (char)c : '?';
		}
		p++;
		if (n + 1 < buflen) {
			buf[n++] = out;
		} else {
			cut = true;
		}
	}
	buf[n] = '\0';
	if (truncated != NULL) {
		*truncated = cut;
	}
	return true;
}

bool msg_get_double(const char *json, const char *key, double *out)
{
	const char *p = after_colon(json, key);
	if (p == NULL) {
		return false;
	}
	while (*p == ' ' || *p == '\t') {
		p++;
	}
	if (*p == '"') {           /* a string value, not a number */
		return false;
	}
	char *end = NULL;
	double v = strtod(p, &end);
	if (end == p) {
		return false;
	}
	*out = v;
	return true;
}


bool msg_get_bool(const char *json, const char *key, bool *out)
{
	const char *p = after_colon(json, key);

	if (p == NULL) {
		return false;
	}
	while (*p == ' ' || *p == '\t') {
		p++;
	}
	if (*p == '"') {           /* a string value, not a boolean */
		return false;
	}
	if (strncmp(p, "true", 4) == 0) {
		*out = true;
		return true;
	}
	if (strncmp(p, "false", 5) == 0) {
		*out = false;
		return true;
	}
	return false;
}
