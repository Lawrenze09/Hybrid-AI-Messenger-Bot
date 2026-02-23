# Ace Apparel Messenger Bot (Gemini AI Integrated)

The Ace Apparel Messenger Bot is an intelligent chatbot developed for Facebook Messenger. It utilizes the Google Gemini AI to provide natural and human-like responses to customer inquiries regarding clothing, orders, and other services offered by Ace Apparel.

## Status: LIVE AND PUBLISHED
The application has officially passed the Meta App Review and is currently in **Live Mode**. It is now available for the public to interact with on Facebook.

## Tech Stack and Architecture
* **Backend**: Flask (Python) deployed on Render.
* **AI Engine**: Google Gemini Pro API for intelligent conversations.
* **Platform**: Meta Graph API (Messenger Platform v25.0).
* **Hosting**: Render (Web Service) featuring automatic deployment from GitHub.

## Summary of Implementation Steps

### 1. Backend Development and Deployment
* Developed a core Flask application to process incoming Webhooks from Meta.
* Integrated Gemini API Keys via **Environment Variables** to ensure maximum security.
* Deployed the production build to Render: `https://ace-apparel-bot-test.onrender.com`.

### 2. Meta Dashboard Configuration
* **Webhooks**: Successfully verified the handshake between Meta and Render using a secure `VERIFY_TOKEN`.
* **Permissions**: Configured the App to subscribe to `messages`, `messaging_postbacks`, and `messaging_referrals`.
* **Identity**: Set up the official brand assets and linked the required Privacy Policy.

### 3. Privacy and Compliance
* Hosted a static Privacy Policy via GitHub Pages: [Privacy Policy Link](https://lawrenze09.github.io/Ace-Apparel-bot-test/privacy.html).
* Fulfilled Meta's Data Protection Officer (DPO) requirements for global compliance.

### 4. Official Publishing
* After meeting all platform requirements, the application was transitioned to **Live Mode**.

## Repository Structure
* `messenger_bot_test.py`: Main application logic and AI integration.
* `privacy.html`: Official privacy documentation for the bot.
* `requirements.txt`: Python dependencies (Flask, google-generativeai, etc.).
* `Procfile`: Deployment instructions for the Render environment.
