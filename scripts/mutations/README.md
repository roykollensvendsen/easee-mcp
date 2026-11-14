# One row per rule

A rule nothing catches is a rule nothing protects. `scripts/mutate.py` turns
each rule off in turn and runs the suite; a rule whose removal breaks no test
has no test. `tests/test_rules_have_evidence.py` counts the rules in the source
against the rows here, so this folder cannot fall behind.

Each row is a file of its own, named after the rule, in a folder that is the
whole path of the file it switches off. `# RULE: a 429 stops calls` in
`src/easee_mcp/cache.py` goes in `src/easee_mcp/cache.py/a-429-stops-calls.toml`
here:

```toml
[[rule]]
name = "a 429 stops calls"
file = "src/easee_mcp/cache.py"
find = 'if until is not None and now < until:'
replace = 'if False:'
```

`find` is a regular expression matching the single line to replace; `replace`
is what replaces it, keeping the file valid. The line's own indentation is
kept, so do not write indentation here. Two pull requests that each add a rule
never meet here, unless they add the same one.
