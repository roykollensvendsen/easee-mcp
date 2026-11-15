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

A rule in `scripts/` is proven the same way, the mutation runner's own
included: the runner has loaded its code before it switches a line off, and
the suite it starts loads the switched-off file. A rule inside `repair()`, or in
any function in the runner it calls by name, could not be proven like this,
since a killed run repairs with the switched-off code;
`test_no_rule_lives_inside_repair` refuses one there. This is garage-door's way.

A test that runs the runner must point `RECOVERY` at its own temporary file.
`tests/conftest.py` fails the whole run if the real recovery file changed.

When the code marks rules and this folder has no rows, the run fails: a folder
emptied by mistake would otherwise leave every rule unproven and the run green.
