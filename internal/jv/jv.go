// Package jv reads decoded JSON (map[string]any, []any, json.Number) the way
// the Python read dictionaries: a missing key or the wrong type is the zero
// value or "absent", never a panic.
package jv

import (
	"encoding/json"
	"strconv"
)

// Obj is v as an object, or nil.
func Obj(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

// List is v as an array, or nil.
func List(v any) []any {
	l, _ := v.([]any)
	return l
}

// Str is m[key] as a string, or "".
func Str(m map[string]any, key string) string {
	s, _ := m[key].(string)
	return s
}

// StrPtr is m[key] as a string, or nil when it is absent or not a string.
func StrPtr(m map[string]any, key string) *string {
	s, ok := m[key].(string)
	if !ok {
		return nil
	}
	return &s
}

// Int is v as an int, or (0, false) when it is not an integer.
func Int(v any) (int, bool) {
	switch n := v.(type) {
	case json.Number:
		i, err := strconv.Atoi(n.String())
		return i, err == nil
	case float64:
		return int(n), n == float64(int(n))
	case int:
		return n, true
	}
	return 0, false
}

// IntOr is m[key] as an int, or fallback.
func IntOr(m map[string]any, key string, fallback int) int {
	if i, ok := Int(m[key]); ok {
		return i
	}
	return fallback
}

// IntPtr is m[key] as an int, or nil: GraphQL's `totalCount`, which the
// checks must not read as zero when it is missing.
func IntPtr(m map[string]any, key string) *int {
	i, ok := Int(m[key])
	if !ok {
		return nil
	}
	return &i
}

// Bool is m[key] as a bool, or false.
func Bool(m map[string]any, key string) bool {
	b, _ := m[key].(bool)
	return b
}
