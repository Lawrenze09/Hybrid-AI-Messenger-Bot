# Ace Apparel Messenger Bot (Gemini AI Integrated)

The Ace Apparel Messenger Bot is an intelligent chatbot developed for Facebook Messenger. It utilizes the Google Gemini AI to provide natural and human-like responses to customer inquiries regarding clothing, orders, and other services offered by Ace Apparel.

## Status: LIVE AND PUBLISHED
The application has officially passed the Meta App Review and is currently in Live Mode. It is now available for the public to interact with on Facebook.

---

## Tech Stack and Architecture
* **Backend**: Flask (Python) deployed on Render.
* **AI Engine**: Google Gemini Pro API for intelligent conversations.
* **Platform**: Meta Graph API (Messenger Platform v25.0).
* **Hosting**: Render (Web Service) featuring automatic deployment from GitHub.

---

## Summary of Implementation Steps

### 1. Backend Development and Deployment
* Developed the core Flask application to handle Webhooks from Meta.
* Integrated the Gemini API Key into Environment Variables for enhanced security.
* Deployed the code to Render using the service URL: `https://ace-apparel-bot-test.onrender.com`.

### 2. Meta Dashboard Configuration
* **Webhooks Setup**: Verified the connection between Meta and Render using a secure `VERIFY_TOKEN`.
* **Permissions**: Subscribed the Page to the `messages`, `messaging_postbacks`, and `messaging_referrals` fields.
* **App Profile**: Uploaded the official 1024x1024 app icon and linked the Privacy Policy.

### 3. Privacy and Compliance
* Created a static Privacy Policy page using GitHub Pages: `https://lawrenze09.github.io/Ace-Apparel-bot-test/privacy.html`.
* Completed the Data Protection Officer information to comply with Meta requirements.

### 4. Official Publishing
* After fulfilling all requirements (Icon, Policy, and Category), the application was successfully transitioned from Development Mode to Live Mode.

---

## Repository Structure
* **app.py**: Contains the main logic for the bot.
* **privacy.html**: The official privacy policy webpage.
* **requirements.txt**: List of dependencies including Flask and google-generativeai.
