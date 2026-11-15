# The bridge's plugin manifest format

`plugin-manifest-v1.json` is a copy of `docs/plugin-manifest/v1.json` in
[claude-voice](https://github.com/roykollensvendsen/claude-voice), taken at its
commit `a18c8ee439bc4a57f01a2469f4e2f1b1fbab19fd`. `tests/test_plugin_manifest.py`
validates this plugin's `claude-voice-plugin.toml` against it. The copy is kept
here so the test never reaches the network; when the bridge changes the format,
take a new copy and name its commit here.
