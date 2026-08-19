# Auto-updating Strava dashboard

A self-hosted version of your training dashboard. A scheduled GitHub Action
pulls fresh data from the Strava API every 6 hours, regenerates
`docs/index.html`, and GitHub Pages serves it — so you get a link you can
open (or add to your phone's home screen) that stays current on its own.

## Setup (about 10 minutes, one time)

### 1. Create a Strava API application
1. Go to https://www.strava.com/settings/api
2. Create an app (any name/website works — e.g. "My Dashboard").
3. Note your **Client ID** and **Client Secret**.
4. Set "Authorization Callback Domain" to `localhost`.

### 2. Get a refresh token
Strava requires a one-time OAuth authorization to get a refresh token that
your Action will use going forward.

1. Visit this URL in your browser, replacing `YOUR_CLIENT_ID`:
   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all,profile:read_all
   ```
2. Click "Authorize." You'll land on a `localhost` page that fails to load
   — that's fine. Copy the `code` value from the URL in your browser's
   address bar.
3. Exchange that code for tokens (run this in a terminal, or use any HTTP
   tool):
   ```bash
   curl -X POST https://www.strava.com/oauth/token \
     -d client_id=YOUR_CLIENT_ID \
     -d client_secret=YOUR_CLIENT_SECRET \
     -d code=THE_CODE_FROM_STEP_2 \
     -d grant_type=authorization_code
   ```
4. The response includes a `refresh_token` — save it.

### 3. Create the GitHub repo
1. Create a new **public** repo (Pages needs public on the free tier, or
   private + GitHub Pro).
2. Push these files (`build_dashboard.py`, `.github/workflows/update-dashboard.yml`,
   this README) to the repo's `main` branch.

### 4. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add all three:
- `STRAVA_CLIENT_ID`
- `STRAVA_CLIENT_SECRET`
- `STRAVA_REFRESH_TOKEN`

### 5. Turn on GitHub Pages
**Settings → Pages** → Source: "Deploy from a branch" → Branch: `main`,
folder: `/docs`. Save.

### 6. Run it once
**Actions tab → Update Strava Dashboard → Run workflow** (this triggers it
manually instead of waiting for the schedule). After it finishes, your
dashboard is live at:
```
https://YOUR_USERNAME.github.io/YOUR_REPO_NAME/
```

Open that link on your phone and add it to your home screen — it'll look
and behave like an app, and refresh with new data every 6 hours automatically.

## Notes
- Change the schedule by editing the `cron` line in the workflow file.
- Refresh tokens don't expire unless you revoke app access on Strava, so
  this should keep running indefinitely without you touching it.
- Everything runs on GitHub's free tier — no server, no cost.
