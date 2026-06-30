# Deployment Setup

Complete runbook for deploying this service to Google Cloud Run via GitHub Actions.
Run these steps once when setting up a new environment from scratch.

## Service Accounts & Permissions

**`github-deployer`** — *(one-time per GCP project)* — Used by GitHub Actions to build and deploy
- `roles/run.admin` — Cloud Run Admin
- `roles/cloudbuild.builds.editor` — Cloud Build Editor
- `roles/artifactregistry.admin` — Artifact Registry Admin *(Writer is insufficient — Admin needed to create the registry on first deploy)*
- `roles/storage.admin` — Storage Admin *(objectAdmin is insufficient — Admin needed to create the GCS staging bucket on first deploy)*
- `roles/iam.serviceAccountUser` — Service Account User

**`<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com`** — *(one-time per GCP project; auto-created by GCP)* — Used by Cloud Build to build and push images
- `roles/run.admin` — Cloud Run Admin
- `roles/artifactregistry.writer` — Artifact Registry Writer
- `roles/iam.serviceAccountUser` — Service Account User *(must be re-granted on each new runtime SA)*

**`inverter-logger-runtime`** — *(one per Cloud Run service)* — Used by the running Cloud Run service to access GCP resources
- `roles/secretmanager.secretAccessor` — Secret Manager Secret Accessor
- `roles/bigquery.dataEditor` — BigQuery Data Editor
- `roles/bigquery.jobUser` — BigQuery Job User
- `roles/run.invoker` — Cloud Run Invoker *(granted post first deploy; required for Cloud Scheduler OIDC auth)*

## Deploying a New Service

When adding a second Cloud Run service to this project, skip steps 1–4 and run only:

- **Step 5** — Create a new runtime SA for the new service (e.g., `new-service-runtime`) and grant it the same roles
- **Step 4 (partial)** — Grant Cloud Build `roles/iam.serviceAccountUser` on the new runtime SA
- **Step 6** — First deploy, then grant `roles/run.invoker` on the new service
- **Step 7** — Create a new Cloud Scheduler job pointing to the new service URL

> **WIF note:** The provider created in Step 3 is locked to the `allweare-org/inverter-logger` repo via `--attribute-condition`. For a different GitHub repo, create a second provider with the new repo's condition, then add a matching `principalSet` binding on `github-deployer`.

---

## Before You Begin

You will need your GCP project number in several commands below. To find it:

```bash
gcloud projects describe all-we-are-master-database --format="value(projectNumber)"
```

Replace `<PROJECT_NUMBER>` in all commands below with that value.

## 1. Enable Required APIs

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project=all-we-are-master-database
```

## 2. Deployer Service Account (`github-deployer`)

Used by GitHub Actions to build and deploy. Auto-created by WIF — you do not set credentials manually.

```bash
gcloud iam service-accounts create github-deployer \
  --project=all-we-are-master-database

gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:github-deployer@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:github-deployer@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.editor"

# artifactregistry.admin (not writer) — needed to create the repo on first deploy
gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:github-deployer@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.admin"

# storage.admin (not objectAdmin) — needed to create the GCS staging bucket on first deploy
gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:github-deployer@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/storage.admin"

gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:github-deployer@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

## 3. Workload Identity Federation (WIF)

Allows GitHub Actions to authenticate as `github-deployer` without storing a JSON key.

```bash
gcloud iam workload-identity-pools create github-pool \
  --project=all-we-are-master-database \
  --location=global

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project=all-we-are-master-database \
  --location=global \
  --workload-identity-pool=github-pool \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="attribute.repository == 'allweare-org/inverter-logger'"

# IMPORTANT: the principalSet value must exactly match the GitHub org/repo name —
# even a single character typo causes silent auth failures.
gcloud iam service-accounts add-iam-policy-binding \
  github-deployer@all-we-are-master-database.iam.gserviceaccount.com \
  --project=all-we-are-master-database \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/attribute.repository/allweare-org/inverter-logger"
```

Verify the binding was created correctly:
```bash
gcloud iam service-accounts get-iam-policy \
  github-deployer@all-we-are-master-database.iam.gserviceaccount.com \
  --project=all-we-are-master-database
```

### GitHub Secrets

Add these two secrets to the repo (Settings → Secrets → Actions):

| Secret | Value |
|---|---|
| `WIF_PROVIDER` | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| `WIF_SERVICE_ACCOUNT` | `github-deployer@all-we-are-master-database.iam.gserviceaccount.com` |

## 4. Cloud Build Service Account

Auto-created when the Cloud Build API is enabled (`<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com`).
Grant it the permissions it needs to build and deploy:

```bash
gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"

# Allow Cloud Build to configure Cloud Run to use the runtime SA
gcloud iam service-accounts add-iam-policy-binding \
  inverter-logger-runtime@all-we-are-master-database.iam.gserviceaccount.com \
  --project=all-we-are-master-database \
  --member="serviceAccount:<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

## 5. Runtime Service Account (`inverter-logger-runtime`)

Used by the running Cloud Run service to access GCP resources.

```bash
gcloud iam service-accounts create inverter-logger-runtime \
  --project=all-we-are-master-database

gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:inverter-logger-runtime@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:inverter-logger-runtime@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding all-we-are-master-database \
  --member="serviceAccount:inverter-logger-runtime@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

## 6. First Deploy

Push to `main` to trigger the GitHub Actions workflow. After the service is created:

```bash
# Get the Cloud Run service URL
gcloud run services describe inverter-logger \
  --region=us-east4 \
  --project=all-we-are-master-database \
  --format="value(status.url)"

# Allow the runtime SA to invoke the service (required for Cloud Scheduler OIDC auth)
gcloud run services add-iam-policy-binding inverter-logger \
  --region=us-east4 \
  --project=all-we-are-master-database \
  --member="serviceAccount:inverter-logger-runtime@all-we-are-master-database.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 7. Cloud Scheduler

Replace `<SERVICE_URL>` with the URL from Step 6:

```bash
gcloud scheduler jobs create http inverter-logger-daily \
  --project=all-we-are-master-database \
  --location=us-east1 \
  --schedule="0 3 * * *" \
  --time-zone="America/New_York" \
  --uri="<SERVICE_URL>/" \
  --message-body='{"backfill_days": 2}' \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --oidc-service-account-email="inverter-logger-runtime@all-we-are-master-database.iam.gserviceaccount.com"
```
