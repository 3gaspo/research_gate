Automatic ArXiv scraper to receive by email the latest submitted articles.

To parameterize your own:

- Fork the repository to your space
- Go to settings 🡢 secrets & variables, set the following:
  SMTP_SERVER = smtp.gmail.com
  SMTP_PORT   = 465
  SMTP_USERNAME = you@gmail.com
  TO_EMAIL = your destination email
  FROM_EMAIL = same or custom “From”
  SMTP_PASSWORD = your Gmail App Password (no spaces)

  (for the smtp password look into : [gmail support](https://support.google.com/mail/answer/185833)

- Go to .github/workflows/arxiv-weekly.yml 🡢 View Runs 🡢 Run workflow

You may also edit the default settings in the .yml
