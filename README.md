# DevMode Policy Tool

Fetch, inspect, and override ChromeOS **user** policy on a managed account.

Works with the on-disk DM policy blob and (optionally) injects local changes so they stick even for cloud-mandatory policies.

## Requirements

- ChromeOS in **developer mode** (or maybe just VT2 root)
- Managed user signed in
- `python3`
- For inject/apply: `cryptography`
- `iptables` / `ip6tables` for `dm-block` / `apply`
- Root shell (VT2 root needs no sudo; chronos needs sudo)

If `python3` or base tools are missing then try:

```bash
sudo dev_install
```

## Quick start

Open a root shell (VT2, `Ctrl+Alt+F2`, log in as `root`), then paste:

```bash
mkdir -p /usr/local/bin && curl -fsSL -o /usr/local/bin/dm_policy_tool https://raw.githubusercontent.com/nmsjayden/dev/main/dm_policy_tool.py && chmod +x /usr/local/bin/dm_policy_tool && /usr/local/bin/dm_policy_tool
```

That installs it and opens the menu. Next time, just type `dm_policy_tool`. Not on a root shell? Put `sudo` in front. Re-run the line above to update.

## Common commands

```bash
dm_policy_tool status          # what’s active right now
dm_policy_tool fetch           # pull current policy from DM, snapshot it
dm_policy_tool dump            # decode latest snapshot
dm_policy_tool list            # search known policy names
dm_policy_tool list Foo        # filter by substring
dm_policy_tool get PolicyName  # value on the live blob
dm_policy_tool toggle PolicyName
dm_policy_tool unset PolicyName
dm_policy_tool update          # check GitHub for a newer version, install it in place
```

### Inject

```bash
# set one or more policies on disk (does not push to Chrome yet)
dm_policy_tool inject --set SomePolicy=true --set OtherPolicy=false

# push live without full sign-out (needs key already trusted once)
dm_policy_tool apply

# or end the session so the next sign-in loads the new key/blob (to get a trusted key)
dm_policy_tool sign-out

# undo inject (auto-backs-up your current changes first, see below)
dm_policy_tool eject
```

`inject` only writes the blob + verification key. Chrome picks it up after `apply` or a fresh sign-in.

### Backups

`eject` automatically saves your current overrides as a profile before it undoes them, so you don't lose an edit you want back later:

```bash
dm_policy_tool backup             # save current overrides without ejecting
dm_policy_tool profiles           # list saved backups (and any other profiles)
dm_policy_tool inject --profile backup-eject-20260101T000000 --force
```

In the interactive menu, open Profiles on any saved profile and press `[s]` to save your current live changes into it.

### Local JSON overrides (managed/)

```bash
dm_policy_tool edit --name default --set PolicyName=true
dm_policy_tool local apply default
dm_policy_tool local list
dm_policy_tool local clear
```

Local managed JSON is merged by Chrome; inject is stronger and can override cloud-mandatory settings.

## Modes (short)

| Mode   | Meaning |
|--------|--------------------------------------------------------------------------------|
| Server | Only DM policy                                                                 |
| Local  | JSON under `/etc/opt/chrome/policies/managed/` (local wins on overlap)         |
| Inject | Overrides written into the session_manager blob and re-signed with a local key |

## State

Under `/root/policy_editor_state/` (or `/usr/local/policy_editor_state/` if `/root` is read-only; override with `DM_POLICY_STATE`):

- `managed-user/` — snapshots, inject key, inject state
- `profiles/` — saved local-override profiles

## Mapping

Policy name ↔ field number comes from Chromium’s `policies.yaml` (with the usual +2 offset for top-level ids).

```bash
dm_policy_tool refresh-mapping
dm_policy_tool verify-mapping --export /path/to/chrome-policy-export.json
dm_policy_tool fix-mapping FIELDNAME CorrectPolicyName
```

If chromium.googlesource.com is down (503), `refresh-mapping` falls back to `policy_mapping_fallback.json` in this repo automatically. That file is a saved copy, not live, so refresh and re-push it occasionally:

```bash
dm_policy_tool refresh-mapping --save-fallback policy_mapping_fallback.json
```

## Safety notes

- `local apply` writes to `/etc/opt/chrome/policies/managed` and affects **every** user on the device. Use `inject` and `apply` to edit the current user.
- A ChromeOS update replaces the system partition: `/root`, the root password and rootfs changes are lost. Install under `/usr/local` (the quick start does) and re-run it after an update.
- This is for **your** managed device in dev mode. Misuse can lock you out of features or break sign-in until you `eject` / restore.
- After `apply`, outbound access to the DM server is blocked (`dm-block`) so background refreshes don’t fail signature checks and report. `eject` / `sign-out` clear that.
- Prefer `eject` before `fetch` if you’ve injected, so snapshots stay genuine.

## License / disclaimer

This tool does **NOT** work with Device policies, only local user policies. 

Use at your own risk. Not affiliated with Google. Intended for debugging and testing on devices you administer.

Inspired by [Pollen](https://github.com/MercuryWorkshop/Pollen), [lilac](https://github.com/MercuryWorkshop/lilac), and [Modmium](https://github.com/CrOSmium/modmium/).
-# AI usage: Readme and changelogs generated by the big gpt bc I'm too lazy, looks like it covers everything correctly tho so if any issues lmk

## Support

Solo project. If something breaks, open an issue. Otherwise, DM Nmsjayden on discord.
