# Ace Apparel Messenger Bot (Gemini AI Integrated)

The Ace Apparel Messenger Bot is a production-ready, intelligent chatbot developed for the Facebook Messenger platform. It leverages a Hybrid AI-Human Architecture, utilizing the Google Gemini API for automated customer service and a custom Handover Protocol for seamless human intervention.

## Status: LIVE AND PUBLISHED
The application has successfully passed the Meta App Review and is currently in Live Mode. It is fully operational and serves public inquiries for the official Ace Apparel Facebook Page.

## Tech Stack and Architecture
* **Backend**: Flask (Python)
* **Deployment**: Render (PaaS) with CI/CD integration.
* **AI Engine**: Google Gemini 3 Flash Preview (Generative AI for NLP).
* **Platform**: Meta Graph API (Messenger Platform v21.0).
* **Database**: In-memory state management for session-based handover.

---

## Key Features and Implementation

### 1. Hybrid AI-Human Handover Protocol
This is a critical business feature designed to balance automation with personalized service:

* **Automated Mode**: The system handles the majority of FAQs using Gemini AI and keyword-based triggers.
* **Handover Trigger (#admin)**: Admins can pause the AI logic instantly by typing #admin in the chat, allowing for manual takeover without bot interference.
* **Bot Reactivation (#bot)**: Admins can resume automation once the manual inquiry is resolved.
* **Graceful Fallback**: If the AI API reaches capacity or encounters an error, the system automatically notifies the user and triggers an internal redirect to a Customer Success Specialist.

### 2. Intelligent NLP and Contextual Responses
* **Custom Brand Context**: The AI is fine-tuned via system prompting to adhere to the brand's unique "Street-smart Taglish" communication style.
* **Payload Handling**: Integrated support for Messenger Postbacks and Quick Replies to guide users through the Sales Funnel (Price Inquiry, Order Process, and Shipping Information).

### 3. Enterprise-Grade Security and Compliance
* **Environment Security**: Sensitive credentials (API Keys, Tokens) are managed via Render Environment Variables to prevent unauthorized access.
* **Webhook Validation**: Implemented a secure SHA-256 handshake for Meta Webhook verification.
* **Regulatory Compliance**: Fully compliant with Meta's Data Privacy policies, featuring a hosted Privacy Policy via GitHub Pages.

---

## Summary of Implementation Steps

### 1. Backend Development
* Developed a scalable Flask core to handle asynchronous Webhook events.
* Implemented robust exception handling to ensure system reliability during API downtime.

### 2. Meta Dashboard and API Integration
* **Webhooks**: Verified the secure handshake between the Meta platform and the Render server.
* **Permissions**: Configured `pages_messaging` and `messaging_postbacks` for full interactive capabilities.
* **App Review**: Successfully navigated the Meta App Review process for public deployment.

### 3. CI/CD and Hosting
* Configured automatic deployment from GitHub to Render, ensuring that code updates are reflected in the production environment in real-time.

---

## Repository Structure
* **messenger_bot_test.py**: Core application logic, AI integration, and Handover Protocol.
* **privacy.html**: Official privacy documentation required for Meta compliance.
* **requirements.txt**: Python dependencies (Flask, google-generativeai, requests).
* **Procfile**: Deployment configuration for the Render environment.

---

## Developer Notes
This project was built to demonstrate the integration of Generative AI into real-world business workflows, focusing on reducing response latency while maintaining high-quality service through managed handovers.
