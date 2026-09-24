# Competitor Watch

Monitors Hungry Monkey delivery availability and posts state changes to Slack.

Alert window: 09:00–23:30 Europe/Gibraltar, daily. GitHub Actions attempts a check every ten minutes during the wider UTC window; scheduled runs can be delayed or skipped.

Secret: `SLACK_WEBHOOK_URL`. The webhook is configured for Rock Hero’s competitor-watch channel. Never commit the webhook value.

The monitor checks multiple venues, reports closures/reopenings and delay warnings, and retains screenshot evidence as a run artifact for three days. A lack of readable evidence should be treated as unknown rather than confirmation. A generic closure does not establish why deliveries stopped.

Use Actions → Competitor watch → Run workflow with the test notification option to test Slack. Without that option it runs a real check.

Site changes and delivery failures require maintenance. Check failed runs in GitHub Actions.
