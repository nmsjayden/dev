# DevMode Policy Tool

Fetch, inspect, and override ChromeOS **user** policy on a managed account.

Works with the on-disk DM policy blob and (optionally) injects local changes so they stick even for cloud-mandatory policies.

## Requirements

- ChromeOS in **developer mode** (or maybe just VT2 root)
- Managed user signed in
- `python3`
- For inject/apply: `cryptography`
- `iptables` / `ip6tables` for `dm-block` / `apply`
- Root for **VT2**

If `python3` or base tools are missing then try:

```bash
sudo dev_install
```

## Quick start

```bash
# curl, save and run (do not run directly from curl unless using args "curl -fsSL <url> | python -" )
curl -fsSL -o /tmp/dm_policy_tool.py https://raw.githubusercontent.com/nmsjayden/dev/main/dm_policy_tool.py && python3 /tmp/dm_policy_tool.py
```

(No args opens the interactive menu.)

## Common commands

```bash
python3 dm_policy_tool.py status          # what’s active right now
python3 dm_policy_tool.py fetch           # pull current policy from DM, snapshot it
python3 dm_policy_tool.py dump            # decode latest snapshot
python3 dm_policy_tool.py list            # search known policy names
python3 dm_policy_tool.py list Foo        # filter by substring
python3 dm_policy_tool.py get PolicyName  # value on the live blob
python3 dm_policy_tool.py toggle PolicyName
python3 dm_policy_tool.py unset PolicyName
```

### Inject

```bash
# set one or more policies on disk (does not push to Chrome yet)
python3 dm_policy_tool.py inject --set SomePolicy=true --set OtherPolicy=false

# push live without full sign-out (needs key already trusted once)
python3 dm_policy_tool.py apply

# or end the session so the next sign-in loads the new key/blob (to get a trusted key)
python3 dm_policy_tool.py sign-out

# undo inject
python3 dm_policy_tool.py eject
```

`inject` only writes the blob + verification key. Chrome picks it up after `apply` or a fresh sign-in.

### Local JSON overrides (managed/)

```bash
python3 dm_policy_tool.py edit --name default --set PolicyName=true
python3 dm_policy_tool.py local apply default
python3 dm_policy_tool.py local list
python3 dm_policy_tool.py local clear
```

Local managed JSON is merged by Chrome; inject is stronger and can override cloud-mandatory settings.

## Modes (short)

| Mode   | Meaning |
|--------|--------------------------------------------------------------------------------|
| Server | Only DM policy                                                                 |
| Local  | JSON under `/etc/opt/chrome/policies/managed/` (local wins on overlap)         |
| Inject | Overrides written into the session_manager blob and re-signed with a local key |

## State

Under `/root/policy_editor_state/`:

- `managed-user/` — snapshots, inject key, inject state
- `profiles/` — saved local-override profiles

## Mapping

Policy name ↔ field number comes from Chromium’s `policies.yaml` (with the usual +2 offset for top-level ids).

```bash
python3 dm_policy_tool.py refresh-mapping
python3 dm_policy_tool.py verify-mapping --export /path/to/chrome-policy-export.json
python3 dm_policy_tool.py fix-mapping FIELDNAME CorrectPolicyName
```

Policies with yaml id > 1040 live in chunked sub-messages and are not supported for inject.

## Safety notes

- This is for **your** managed device in dev mode. Misuse can lock you out of features or break sign-in until you `eject` / restore.
- After `apply`, outbound access to the DM server is blocked (`dm-block`) so background refreshes don’t fail signature checks and report. `eject` / `sign-out` clear that.
- Prefer `eject` before `fetch` if you’ve injected, so snapshots stay genuine.

## License / disclaimer

This tool does **NOT** work with Device policies, only local user policies. 

Use at your own risk. Not affiliated with Google. Intended for debugging and testing on devices you administer.

Inspired by [Pollen](https://github.com/MercuryWorkshop/Pollen), [lilac](https://github.com/MercuryWorkshop/lilac), and [Modmium](https://github.com/CrOSmium/modmium/).

## Support

Solo project. If something breaks, open an issue. Otherwise, DM Nmsjayden on discord.
