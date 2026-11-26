// Package pyfmt reproduces the handful of Python formatting rules the digest,
// the findings and the state document were written with.
//
// The Go port must produce the same bytes the Python one did: a finding's
// detail quotes PR titles with repr(), the footer groups digits with {:,},
// the state blob is json.dumps(indent=2), and the HTML goes through
// html.escape. Each of those differs from Go's nearest standard equivalent in
// some corner (quote choice, '\x27' vs '&#39;', ensure_ascii), and a corner
// that differs is a digest that changes for no reason on the day of the
// cutover. Tested against strings the Python produced.
package pyfmt

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// Repr is Python's repr() of a str: single quotes unless the text contains a
// single quote and no double quote, with Python's escapes for the rest.
func Repr(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, r := range s {
		switch {
		case r == quote || r == '\\':
			b.WriteRune('\\')
			b.WriteRune(r)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r < 0x7f:
			b.WriteRune(r)
		case !unicode.IsPrint(r):
			switch {
			case r <= 0xff:
				fmt.Fprintf(&b, `\x%02x`, r)
			case r <= 0xffff:
				fmt.Fprintf(&b, `\u%04x`, r)
			default:
				fmt.Fprintf(&b, `\U%08x`, r)
			}
		default:
			b.WriteRune(r)
		}
	}
	b.WriteRune(quote)
	return b.String()
}

// Thousands is Python's f"{n:,}".
func Thousands(n int) string {
	digits := strconv.Itoa(n)
	sign := ""
	if strings.HasPrefix(digits, "-") {
		sign, digits = "-", digits[1:]
	}
	var b strings.Builder
	for i, d := range digits {
		if i > 0 && (len(digits)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(d)
	}
	return sign + b.String()
}

// SplitLines is Python's str.splitlines(): every line boundary Python
// recognises, no trailing empty line.
func SplitLines(s string) []string {
	var lines []string
	start := 0
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		switch r {
		case '\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', '\u2028', '\u2029':
			lines = append(lines, s[start:i])
			i += size
			start = i
		case '\r':
			lines = append(lines, s[start:i])
			i += size
			if i < len(s) && s[i] == '\n' {
				i++
			}
			start = i
		default:
			i += size
		}
	}
	if start < len(s) {
		lines = append(lines, s[start:])
	}
	return lines
}

// Truncate keeps the first n characters (code points, as Python's slicing
// does), not bytes, so a cut never splits a character.
func Truncate(s string, n int) (string, bool) {
	if utf8.RuneCountInString(s) <= n {
		return s, false
	}
	count := 0
	for i := range s {
		if count == n {
			return s[:i], true
		}
		count++
	}
	return s, false
}

// Len is Python's len() of a str.
func Len(s string) int { return utf8.RuneCountInString(s) }

// EscapeHTML is Python's html.escape(s, quote=True).
func EscapeHTML(s string) string {
	return htmlEscaper.Replace(s)
}

var htmlEscaper = strings.NewReplacer(
	"&", "&amp;",
	"<", "&lt;",
	">", "&gt;",
	`"`, "&quot;",
	"'", "&#x27;",
)

// KV is one key of an ordered JSON object.
type KV struct {
	Key   string
	Value any
}

// Object is a JSON object whose keys keep their order, as a Python dict does.
type Object []KV

// JSON is Python's json.dumps with ensure_ascii=True. indent > 0 is
// json.dumps(indent=n), whose separators are "," and ": "; indent 0 is
// separators=(",", ":"), the compact form the triage prompt is sent in.
//
// Values may be nil, bool, int, string, *string, *int, []string, []any,
// Object or map[string]any (written with sorted keys).
func JSON(value any, indent int) string {
	var b strings.Builder
	writeJSON(&b, value, indent, 0)
	return b.String()
}

func writeJSON(b *strings.Builder, value any, indent, depth int) {
	switch v := value.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if v {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case int:
		b.WriteString(strconv.Itoa(v))
	case string:
		writeString(b, v)
	case *string:
		if v == nil {
			b.WriteString("null")
		} else {
			writeString(b, *v)
		}
	case *int:
		if v == nil {
			b.WriteString("null")
		} else {
			b.WriteString(strconv.Itoa(*v))
		}
	case []string:
		items := make([]any, len(v))
		for i, s := range v {
			items[i] = s
		}
		writeArray(b, items, indent, depth)
	case []any:
		writeArray(b, v, indent, depth)
	case Object:
		writeObject(b, v, indent, depth)
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		obj := make(Object, len(keys))
		for i, k := range keys {
			obj[i] = KV{k, v[k]}
		}
		writeObject(b, obj, indent, depth)
	default:
		panic(fmt.Sprintf("pyfmt.JSON: unsupported type %T", value))
	}
}

func newline(b *strings.Builder, indent, depth int) {
	if indent > 0 {
		b.WriteByte('\n')
		b.WriteString(strings.Repeat(" ", indent*depth))
	}
}

func writeArray(b *strings.Builder, items []any, indent, depth int) {
	if len(items) == 0 {
		b.WriteString("[]")
		return
	}
	b.WriteByte('[')
	for i, item := range items {
		if i > 0 {
			b.WriteByte(',')
		}
		newline(b, indent, depth+1)
		writeJSON(b, item, indent, depth+1)
	}
	newline(b, indent, depth)
	b.WriteByte(']')
}

func writeObject(b *strings.Builder, obj Object, indent, depth int) {
	if len(obj) == 0 {
		b.WriteString("{}")
		return
	}
	b.WriteByte('{')
	for i, kv := range obj {
		if i > 0 {
			b.WriteByte(',')
		}
		newline(b, indent, depth+1)
		writeString(b, kv.Key)
		if indent > 0 {
			b.WriteString(": ")
		} else {
			b.WriteByte(':')
		}
		writeJSON(b, kv.Value, indent, depth+1)
	}
	newline(b, indent, depth)
	b.WriteByte('}')
}

// writeString is json.dumps's ensure_ascii string encoding.
func writeString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			switch {
			case r < 0x20 || (r > 0x7e && r <= 0xffff):
				fmt.Fprintf(b, `\u%04x`, r)
			case r > 0xffff:
				r -= 0x10000
				fmt.Fprintf(b, `\u%04x\u%04x`, 0xd800+(r>>10), 0xdc00+(r&0x3ff))
			default:
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// ISO is Python's datetime.isoformat() of an aware UTC time: microseconds
// only when there are any, and "+00:00" rather than "Z".
func ISO(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond()/1000 == 0 {
		return t.Format("2006-01-02T15:04:05+00:00")
	}
	return t.Format("2006-01-02T15:04:05.000000+00:00")
}
