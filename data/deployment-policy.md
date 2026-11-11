# Production Deployment Policy

Production deployments must use a rolling deployment strategy.

After deployment, the engineering team must monitor application error rate, p95 latency, and availability.

If the application error rate exceeds 5 percent for five consecutive minutes after a deployment, the deployment must be rolled back to the previous stable version.

The on-call engineer is responsible for initiating the rollback.

Every production rollback must be recorded in the incident Slack channel.

A rollback should be preferred over attempting a live fix when customer impact is ongoing.