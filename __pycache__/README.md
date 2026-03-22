
# 🐟 Finding Dory
## An Intelligent Assistant for the Forgetful.
Finding Dory isn't just another chatbot. It’s a proactive, agentic system built to serve as a "digital companion" for individuals with early-stage dementia. By combining the reasoning power of Claude 3.5 Sonnet with real-world Singaporean data (OneMap), Dory helps users stay safe, remember their loved ones, and keep up with their health.

## The "Tech Behind the Fins"
I built this using a Modular Tool-Calling Architecture. Instead of hard-coding every response, I gave the LLM a "utility belt" of Python scripts it can choose to execute autonomously.

**The Brain:** Powered by **AWS Bedrock**. I implemented a custom llm_router.py that handles model selection (Sonnet for complex reasoning, Haiku for speed) with built-in exponential backoff to handle API rate limits.

**The Memory:** Unlike a typical session-based AI, Dory has a "long-term memory" saved in AWS S3. Every contact, medical record, and personal memory is persisted in JSON format.

**The Safety Net:** Integrated with the **OneMap SG API**. If a user says "I'm lost," Dory doesn't just give advice—it calculates the nearest Polyclinic or MRT station in Singapore and provides a direct Google Maps link.

**The Heart:** A custom Memory Recall Tool that uses images and descriptions to test cognitive health, tracking "mistake counts" to help caregivers monitor progress.

## Architecture
```finding_dory/
├── app.py                     # FastAPI Gateway: Manages API routing
├── streamlit_app.py           # Frontend UI: Interactive dashboard
├── agent_runner.py            # The Executive: Core ReAct loop
├── llm_router.py              # The Brain: Bedrock model routing
├── core/
│   ├── model.py               # AWS Bedrock foundation configurations
│   └── timezone.py            # Localization: Asia/Singapore time logic
└── tools/
    ├── emergency_help_tool.py # OneMap SG API integration
    ├── medical_record_tool.py # Storage: CRUD operations on AWS S3
    ├── med_notification_tool.py # Logic: Medication reminder scheduler
    └── freq_places_tool.py    # Context: Geofencing & location tracking

```
## Prerequisites
Before installing, ensure you have the necessary Python libraries by running
```pip install -r requirements.txt```

## Installation
1. **Clone this repository**
   - Use `git clone [your-repo-link]` or download the ZIP.
2. **Set up the Environment**
   - Create a `.env` file in the root directory and add your keys:
     ```env
     AWS_ACCESS_KEY_ID=your_key_here
     AWS_SECRET_ACCESS_KEY=your_secret_here
     ONEMAP_API_TOKEN=your_token_here
     ```
3. **Launch the API**
   - Open a terminal and run: `uvicorn app:app --reload --port 8000`
4. **Launch the UI**
   - Open a second terminal and run: `streamlit run streamlit_app.py`
5. **Start using Finding Dory**
   - The dashboard will open in your browser automatically. 

## What I Learned
Building this at NTU taught me that AI is only as good as its tools. The hardest part wasn't the AI—it was handling Singapore Timezones (Asia/Singapore) in timzone.py and ensuring the agent didn't "hallucinate" emergency contacts that didn't exist in the S3 database.

## Future Roadmap 
1. [ ] Voice Integration: Adding STT (Speech-to-Text) for easier interaction for elderly users.

2. [ ] Wearable Sync: Connecting to IoT devices for real-time fall detection.

3. [ ] Caregiver SMS Alerts: Integrating Twilio to send automated SMS when a user triggers an emergency tool.