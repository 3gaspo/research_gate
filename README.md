# 📨 ArXiv daily digest

Automatically fetch and email the latest arXiv submissions in a specified field.

---

## ⚙️ Setup

### 1. Fork the repo
Click **Fork** (top right) to copy it to your GitHub account.

---

### 2. Add secrets  
Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Example | Notes |
|------|----------|-------|
| `SMTP_SERVER` | `smtp.gmail.com` | Mail server (Gmail shown) |
| `SMTP_PORT` | `465` | Usually `465` for SSL |
| `SMTP_USERNAME` | `you@gmail.com` | Sender address |
| `SMTP_PASSWORD` | *(Gmail App Password)* | [Create one](https://support.google.com/mail/answer/185833) — **no spaces** |
| `TO_EMAIL` | `recipient@example.com` | Where to send the digest |
| `FROM_EMAIL` | `you@gmail.com` | Appears in “From” field |

---

### 3. Run it  
Go to **Actions → arXiv Weekly Digest → Run workflow**  
You’ll receive an email with recent papers shortly after.

---

### 4. Customize  
Edit `.github/workflows/arxiv-weekly.yml` to change defaults:

| Variable | Default | Description |
|-----------|----------|-------------|
| `ARXIV_CATEGORIES` | `cs.LG` | ArXiv categories |
| `ARXIV_KEYWORDS` | `federated learning,time series` | Keywords in title/abstract |
| `ARXIV_DAYS` | `14` | Look-back window |
| `MAX_RESULTS` | `150` | API fetch limit |
| `INCLUDE_ABSTRACTS` | `false` | Add 1-line abstracts |
| `INTERSECT_KW` | `false` | Must include all keywords |

---

### 5. Schedule  
Runs **every Monday at 07:00 UTC** (default).  
Adjust the `cron:` line in the workflow to change timing.

---
