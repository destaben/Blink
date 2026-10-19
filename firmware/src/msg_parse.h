#ifndef MSG_PARSE_H
#define MSG_PARSE_H

#include <stdbool.h>
#include <stddef.h>

/* Extract the string value for `key` from a flat JSON line into buf.
 * Returns true if found. */
bool msg_get_str(const char *json, const char *key, char *buf, size_t buflen);

/* Like msg_get_str, but undoes JSON escapes (\" \\ \/ \n \t \r \b \f \uXXXX)
 * instead of stopping at the first quote.
 *
 * msg_get_str cuts a value at the first `"` it meets, which is right for the
 * identifiers and enums every other message carries and WRONG for text that
 * somebody else wrote. The approval prompt shows a shell command and asks a
 * person to say yes to it; a command containing a quote would have been shown
 * cut off at that quote, and the person would have approved a command they had
 * not read. So a prompt takes its text through this function and nothing else.
 *
 * Output is printable ASCII plus '\n': a \uXXXX above 0x7e, and any raw byte
 * outside 0x20..0x7e (UTF-8 included), becomes '?'. A '?' is a visible hole in
 * the text; a lookalike letter that renders as the real one is not.
 *
 * Returns false when the key is missing, is not a string, or the string never
 * ends. *truncated is set when `buflen` was too small to hold all of it, and
 * the caller MUST NOT treat a truncated value as the thing the sender meant. */
bool msg_get_str_esc(const char *json, const char *key, char *buf,
		     size_t buflen, bool *truncated);

/* Extract a numeric value for `key`. Returns true if found (sets *out). */
bool msg_get_double(const char *json, const char *key, double *out);

/* Extract a JSON boolean (`true`/`false`) for `key`. Returns true if found
 * (sets *out). A quoted "true" is NOT a boolean and returns false, the same
 * way msg_get_double refuses a quoted number: the daemon emits real JSON
 * booleans, and accepting the string form would let a typo read as valid. */
bool msg_get_bool(const char *json, const char *key, bool *out);

#endif /* MSG_PARSE_H */
