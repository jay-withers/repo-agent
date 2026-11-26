package pyfmt

import (
	"slices"
	"testing"
	"time"
)

// Every expected value below is what CPython 3.15 printed for the same input.

func TestRepr(t *testing.T) {
	cases := [][2]string{
		{"plain", `'plain'`},
		{"it's", `"it's"`},
		{`say "hi"`, `'say "hi"'`},
		{`both ' and "`, `'both \' and "'`},
		{"tab\tnl\nret\r", `'tab\tnl\nret\r'`},
		{"bell\x07 del\x7f", `'bell\x07 del\x7f'`},
		{"nbsp\u00a0 zwsp\u200b", "'nbsp\\xa0 zwsp\\u200b'"},
		{"emoji 😀", `'emoji 😀'`},
		{"ünï — “q”", `'ünï — “q”'`},
		{`back\slash`, `'back\\slash'`},
		{"\u0085next", `'\x85next'`},
		{"\U000e0001tag", `'\U000e0001tag'`},
	}
	for _, c := range cases {
		if got := Repr(c[0]); got != c[1] {
			t.Errorf("Repr(%q) = %s, want %s", c[0], got, c[1])
		}
	}
}

func TestSplitLines(t *testing.T) {
	cases := []struct {
		in   string
		want []string
	}{
		{"a\nb", []string{"a", "b"}},
		{"a\r\nb\rc", []string{"a", "b", "c"}},
		{"a\n", []string{"a"}},
		{"", nil},
		{"\n", []string{""}},
		{"x\vy\fz\x1cw\u2028v\u2029u\u0085t", []string{"x", "y", "z", "w", "v", "u", "t"}},
		{"end\r", []string{"end"}},
	}
	for _, c := range cases {
		if got := SplitLines(c.in); !slices.Equal(got, c.want) {
			t.Errorf("SplitLines(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestJSONMatchesPythonDumps(t *testing.T) {
	value := Object{
		{"a", "\u00fc\U0001F600\u2028<&>"},
		{"b", nil},
		{"c", []any{1, "x"}},
		{"d", Object{}},
		{"e", []string{}},
		{"f", true},
		{"g", false},
		{"h", "\x00\x1f\b\f\t"},
	}
	indented := "{\n  \"a\": \"\\u00fc\\ud83d\\ude00\\u2028<&>\",\n  \"b\": null,\n  \"c\": [\n    1,\n    \"x\"\n  ],\n" +
		"  \"d\": {},\n  \"e\": [],\n  \"f\": true,\n  \"g\": false,\n  \"h\": \"\\u0000\\u001f\\b\\f\\t\"\n}"
	compact := `{"a":"\u00fc\ud83d\ude00\u2028<&>","b":null,"c":[1,"x"],"d":{},"e":[],"f":true,"g":false,"h":"\u0000\u001f\b\f\t"}`
	if got := JSON(value, 2); got != indented {
		t.Errorf("indent=2:\n%s\nwant\n%s", got, indented)
	}
	if got := JSON(value, 0); got != compact {
		t.Errorf("compact:\n%s\nwant\n%s", got, compact)
	}
}

func TestJSONPointersAndMaps(t *testing.T) {
	s, n := "x", 3
	var ns *string
	var ni *int
	got := JSON([]any{&s, ns, &n, ni, map[string]any{"z": 1, "a": `"q"\`}}, 0)
	want := `["x",null,3,null,{"a":"\"q\"\\","z":1}]`
	if got != want {
		t.Errorf("got %s, want %s", got, want)
	}
}

func TestJSONRejectsUnknownTypes(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Error("a float should panic: nothing here should ever write one")
		}
	}()
	JSON(1.5, 0)
}

func TestThousands(t *testing.T) {
	for n, want := range map[int]string{0: "0", 7: "7", 999: "999", 1000: "1,000", 1234567: "1,234,567", -1234567: "-1,234,567"} {
		if got := Thousands(n); got != want {
			t.Errorf("Thousands(%d) = %s, want %s", n, got, want)
		}
	}
}

func TestTruncateCountsCharactersNotBytes(t *testing.T) {
	if got, cut := Truncate("ünïcode", 3); got != "ünï" || !cut {
		t.Errorf("Truncate = %q, %v", got, cut)
	}
	if got, cut := Truncate("abc", 3); got != "abc" || cut {
		t.Errorf("Truncate at the limit = %q, %v", got, cut)
	}
	if Len("ünï") != 3 {
		t.Error("Len counts characters")
	}
}

func TestEscapeHTMLIsPythonsHTMLEscape(t *testing.T) {
	if got := EscapeHTML(`<a href="x">'&'</a>`); got != "&lt;a href=&quot;x&quot;&gt;&#x27;&amp;&#x27;&lt;/a&gt;" {
		t.Errorf("got %s", got)
	}
}

func TestISO(t *testing.T) {
	whole := time.Date(2026, 11, 29, 18, 0, 3, 0, time.UTC)
	if got := ISO(whole); got != "2026-11-29T18:00:03+00:00" {
		t.Errorf("got %s", got)
	}
	if got := ISO(whole.Add(123456 * time.Microsecond)); got != "2026-11-29T18:00:03.123456+00:00" {
		t.Errorf("got %s", got)
	}
}
