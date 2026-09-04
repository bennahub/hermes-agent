# BWM-796 — Files / artifact hosted UAT

Uses the existing computer workspace + dashboard artifact routes. No second
file-sharing system.

## Download

Agent click `#download` on `http://127.0.0.1:8765/artifact.bin`.

Headless Chromium often **navigates** instead of using the download manager.
`HermesChromiumRuntime._maybe_save_loopback_download` fetches the loopback
non-HTML URL (honoring `Content-Disposition`) into
`workspace/downloads/`.

Live result:

- file `bwm796-artifact.txt` size 27
- `GET /api/agent-computers/{id}/artifacts` lists it (`kind=download`)
- `GET .../artifacts/bwm796-artifact.txt?folder=downloads` body
  `BWM-796 synthetic artifact`

Owner received the artifact through Hermes, not by reading a random Chrome
directory.

Wake also sets `Browser.setDownloadBehavior` and
`--download-default-directory={workspace}/downloads` for cases Chrome does use
the download manager.

## Upload

1. Owner `POST /api/agent-computers/{id}/workspace-files` with
   `bwm796-upload.txt`.
2. Agent `act kind=upload target=#file-input text=bwm796-upload.txt`.
3. Basename only; resolved under `workspace/uploads` or `workspace/downloads`.

`DOM.setFileInputFiles` requires one CDP websocket session (`cdp_set_file_input`).
One-shot `loopback_cdp` calls lose `objectId` (`-32000`).

Live result: synthetic `#uploaded` shows `received:bwm796-upload.txt`.

## Boundaries

| Probe | Result |
|---|---|
| `../ok.txt` as upload name | `ValueError` basename |
| Missing workspace file | `authorized workspace file not found` |
| `GET artifacts/..%2Fetc%2Fpasswd` | 404 |
| Identity profile dir | 0700 under `agent-computers/identities/` |
| Host `/etc`, SSH keys, other agents | not reachable via artifact/upload APIs |

This is BWM-796 workspace confinement only — not BWM-795 machine ownership.
