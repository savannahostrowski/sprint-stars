# Sprint Stars

A leaderboard for issues closed, PRs opened, and code reviews across GitHub repositories. Configure it with your team and public repos.

## Getting started

You'll need Python 3.15+ and a GitHub personal access token.

1. **Create your repo.** Select **Use this template → Create a new repository**, then clone your new repo and enter its directory.

2. **Configure your team.** Edit `metadata.toml` with your organizations, repositories, and participants:

   ```toml
   name = "Our team leaderboard"
   organizations = ["our-org"]
   repositories = ["another-org/shared-project"]
   contributors = ["alice", "bob"]
   refresh_minutes = 20
   ```

   Use `organizations = []` to track only specific repos. An org includes all its public repos, including forks and archives. Overlapping repo lists count once.

3. **Set your GitHub token.** [Create a personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token) and set it in the shell where you'll run the app:

   ```sh
   export GITHUB_TOKEN="your-token"
   ```

   Public repository read access is sufficient. Keep the token out of metadata and committed files. The dev server does not load `.env` files automatically.

4. **Start the app.**

   ```sh
   uv sync
   uv run python dev.py
   ```

   Open [localhost:8774](http://127.0.0.1:8774/). The first load fetches your team's data from GitHub.

[metadata.toml](metadata.toml) shows display names, account aliases, timezones, and an optional CSV roster. Use GitHub usernames for contributors. Leave the roster section commented out to rank only the people you list. Restart or redeploy after changing metadata.

## Deploy your own app (optional)

To deploy on [FastAPI Cloud](https://fastapicloud.com/docs/fastapi-cloud-cli/deploy/), run these commands from the repository root:

1. **Create and link your app.**

   ```sh
   uv run fastapi cloud apps create --name your-board
   ```

2. **Set the deployed app's GitHub token.** The CLI will prompt for the token:

   ```sh
   uv run fastapi cloud env set GITHUB_TOKEN --secret --no-redeploy
   ```

3. **Deploy.**

   ```sh
   uv run fastapi deploy
   ```
