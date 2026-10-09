# JobFinder cloud setup files

This bundle adds a GitHub Actions polling version without overwriting the local scripts.

- `auto_jobfinder_cloud.py`: cloud-safe copy of the analysis and application engine.
- `cloud_runner.py`: one-shot polling and processing runner.
- `prepare_cloud_data.py`: locally encrypts CVs, candidate profile, and sent history.
- `requirements-cloud.txt`: minimal dependencies for the cloud job.
- `.github/workflows/jobfinder-cloud.yml`: scheduled GitHub Actions workflow.

Only `.enc` and `.fernet` ciphertext files should be committed from `cloud_private/`. Never commit `.jobfinder_fernet_key`, plaintext CVs, Telegram session strings, or any API/app passwords.

The first workflow run establishes the latest Telegram message ID for each channel and skips older posts. New posts are processed on later runs. GitHub scheduled jobs can run late.
