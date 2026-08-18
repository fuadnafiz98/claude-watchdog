# plugins

Claude Code plugins by [@fuadnafiz98](https://github.com/fuadnafiz98). One marketplace,
one directory per plugin.

```sh
claude plugin marketplace add fuadnafiz98/plugins
claude plugin install watchdog@fuadnafiz98
```

or the same two steps as `/plugin marketplace add …` and `/plugin install …` inside a
session. Note the nesting: it is `claude plugin marketplace`, not `claude marketplace`,
which is not a subcommand and is silently treated as a prompt.

| Plugin | What it does |
| --- | --- |
| [watchdog](watchdog/) | Resumes a turn that a transient API error killed, so an unattended run survives the night instead of waiting for someone to type `continue`. |

Each plugin's own README is the documentation for it; this file is only the index.

## How a release happens

There is nothing to publish to. Users install straight from this repository, so `main`
is the registry and a push is the release — which also means a fix pushed without a
version bump is invisible to anyone who already has the plugin: Claude Code pins an
installed plugin to its `version` and only offers an update when that string changes.

So bumping `version` in `<plugin>/.claude-plugin/plugin.json` is the release. CI does
the rest: [`release.yml`](.github/workflows/release.yml) tags `<plugin>-v<version>` and
cuts a GitHub release from the commits that touched that plugin, and says in the run
summary when a push released nothing.

[`ci.yml`](.github/workflows/ci.yml) runs on every push and pull request: it checks the
marketplace manifest against each plugin's own manifest (names agree, sources resolve,
no version drift), parses every shell script under both `sh` and `dash`, and runs each
plugin's tests. Plugins are discovered from `*/.claude-plugin/plugin.json`, so a new one
is covered without editing the workflows.

## Adding a plugin

```
<name>/
  .claude-plugin/plugin.json    name, version, description  -- the manifest
  tests/test_<name>.py          CI fails a plugin with no tests
  …                             commands/, hooks/, skills/, agents/, scripts/
```

then add an entry to [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json)
with a `source` of `./<name>`. Leave `version` out of that entry — the plugin's own
manifest owns it, and CI rejects the two disagreeing.

## Licence

MIT, see [LICENSE](LICENSE).
