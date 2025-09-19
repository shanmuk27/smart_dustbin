# --- Final Smart Dustbin Server ---
# This version includes all corrections for the rule-based AI coach and data integrity.
# --- VERSION 2: Includes Delete Account, Unlink Dustbin, and Unique Dustbin Linking Logic ---
#pip install -r requirements.txt
#$env:GOOGLE_API_KEY="AIzaSyCUNFKfT8Hbv0808UBIO5U36ZoGBjfUuao"
#python run_dustbin_system.py

import serial
import threading
import time
import firebase_admin
from firebase_admin import credentials, firestore, auth
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from google.oauth2 import service_account
import os
import google.generativeai as genai

# --- CONFIGURATION ---
SERIAL_PORT = 'COM7'
BAUD_RATE = 9600
USER_LOCATION = "Hyderabad, Telangana, India"
SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"

# --- Gemini AI Configuration ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

model = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        print("✅ Gemini AI Model Initialized.")
    except Exception as e:
        print(f"🔥 Gemini Initialization Error: {e}")
        model = None
else:
    print("⚠️  AI Coach disabled. GOOGLE_API_KEY environment variable not found.")


# --- Global State ---
arduino_status = { "status": "Offline", "last_seen": None }

# --- Initialization ---
try:
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
    project_id = creds.project_id
    
    firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT_FILE))
    db = firestore.client()
    print("✅ Successfully connected to Firebase.")
    
except Exception as e:
    print(f"🔥🔥🔥 CRITICAL ERROR during initialization: {e}")
    db = None

app = Flask(__name__)
CORS(app)

# --- Arduino Communication Thread ---
def read_from_arduino():
    while True:
        try:
            with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as ser:
                arduino_status.update({"status": "Connected", "last_seen": time.time()})
                print(f"✅ Arduino connected on {SERIAL_PORT}.")
                
                while True:
                    line = ser.readline().decode('utf-8').strip()
                    if line:
                        arduino_status["last_seen"] = time.time()
                        if line != "hb": # Ignore heartbeat messages
                            print(f"Received from Arduino: {line}")
                            process_arduino_data(line)
                    
                    if time.time() - arduino_status.get("last_seen", 0) > 15:
                        arduino_status["status"] = "Offline"
                        print("⚠️ Arduino connection timed out (no heartbeat).")
                        break
        except serial.SerialException:
            arduino_status["status"] = "Offline"
            print(f"⚠️ Arduino not found on {SERIAL_PORT}. Retrying...")
            time.sleep(5)
        except Exception as e:
            arduino_status["status"] = "Error"
            print(f"🔥 Arduino thread error: {e}")
            time.sleep(5)

def process_arduino_data(data):
    if not db: return
    try:
        dustbin_id, waste_type = data.split(',')
        query = db.collection('users').where('linked_dustbin', '==', dustbin_id).limit(1).stream()
        user_doc = next(query, None)
        if user_doc:
            update_points(user_doc.id, waste_type)
    except Exception as e:
        print(f"Error processing data '{data}': {e}")

def update_points(user_id, waste_type):
    points_map = {'DRY': 5, 'WET': 8, 'EWASTE': 10}
    points = points_map.get(waste_type.upper(), 0)
    if points == 0: return

    user_ref = db.collection('users').document(user_id)
    try:
        user_ref.update({
            f'points.{waste_type.lower()}': firestore.Increment(1),
            'points.total': firestore.Increment(points)
        })
        print(f"Updated points for user {user_id}.")
    except Exception as e:
        print(f"Error updating points for user {user_id}: {e}")

# --- Web Server Routes ---
@app.route("/")
def index():
    return render_template('index.html')

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    try:
        user = auth.create_user(email=data['email'], password=data['password'])
        user_data = {'email': user.email, 'linked_dustbin': None, 'points': {'dry': 0, 'wet': 0, 'ewaste': 0, 'total': 0}}
        db.collection('users').document(user.uid).set(user_data)
        return jsonify({'uid': user.uid}), 201
    except Exception as e:
        if 'EMAIL_EXISTS' in str(e): return jsonify({'error': 'An account with this email already exists.'}), 400
        return jsonify({'error': 'Registration failed.'}), 400

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    try:
        # Note: In a real app, you would verify the password here.
        # This example only checks for email existence for simplicity.
        user = auth.get_user_by_email(data['email'])
        return jsonify({'uid': user.uid}), 200
    except auth.UserNotFoundError:
        return jsonify({'error': 'No account found with that email.'}), 404
    except Exception as e:
        return jsonify({'error': 'An unexpected error occurred.'}), 500

@app.route('/user/<uid>', methods=['GET'])
def get_user(uid):
    try:
        user_ref = db.collection('users').document(uid)
        user_doc = user_ref.get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            points = user_data.get('points', {})
            
            dry_count = points.get('dry', 0)
            wet_count = points.get('wet', 0)
            ewaste_count = points.get('ewaste', 0)
            calculated_total = (dry_count * 5) + (wet_count * 8) + (ewaste_count * 10)
            stored_total = points.get('total', 0)

            if calculated_total != stored_total:
                print(f"Data inconsistency found for user {uid}. Stored: {stored_total}, Real: {calculated_total}. Fixing now...")
                user_ref.update({'points.total': calculated_total})
                user_data['points']['total'] = calculated_total

            return jsonify(user_data), 200
        else: 
            auth_user = auth.get_user(uid)
            user_data = {'email': auth_user.email, 'linked_dustbin': None, 'points': {'dry': 0, 'wet': 0, 'ewaste': 0, 'total': 0}}
            user_ref.set(user_data)
            return jsonify(user_data), 200
    except Exception as e:
        return jsonify({'error': f'Could not retrieve user data: {e}'}), 404

# --- NEW --- Endpoint for deleting a user account
@app.route('/user/<uid>', methods=['DELETE'])
def delete_user(uid):
    try:
        # 1. Delete user from Firebase Authentication
        auth.delete_user(uid)
        
        # 2. Delete user data from Firestore
        db.collection('users').document(uid).delete()
        
        print(f"Successfully deleted user {uid}.")
        return jsonify({'success': True, 'message': f'User {uid} deleted successfully.'}), 200
    except auth.UserNotFoundError:
        return jsonify({'error': 'User not found in Firebase Auth.'}), 404
    except Exception as e:
        print(f"Error deleting user {uid}: {e}")
        return jsonify({'error': f'Failed to delete user: {e}'}), 500

# --- MODIFIED --- Link dustbin route with uniqueness check
@app.route('/link_dustbin', methods=['POST'])
def link_dustbin():
    data = request.json
    uid = data.get('uid')
    dustbin_id = data.get('dustbin_id')

    if not uid or not dustbin_id:
        return jsonify({'error': 'Missing UID or Dustbin ID.'}), 400

    try:
        # Check if the dustbin ID is already linked to another user
        existing_link_query = db.collection('users').where('linked_dustbin', '==', dustbin_id).limit(1).stream()
        linked_doc = next(existing_link_query, None)

        if linked_doc and linked_doc.id != uid:
            return jsonify({'error': 'This dustbin is already linked to another account.'}), 409 # 409 Conflict

        # If not linked, or linked to the same user, proceed to update
        db.collection('users').document(uid).update({'linked_dustbin': dustbin_id})
        return jsonify({'success': True}), 200
    except Exception as e:
        print(f"Error linking dustbin: {e}")
        return jsonify({'error': 'An internal error occurred while linking the dustbin.'}), 500

# --- NEW --- Endpoint for unlinking a dustbin
@app.route('/unlink_dustbin', methods=['POST'])
def unlink_dustbin():
    data = request.json
    uid = data.get('uid')
    if not uid:
        return jsonify({'error': 'Missing UID.'}), 400
    
    try:
        db.collection('users').document(uid).update({'linked_dustbin': None})
        return jsonify({'success': True}), 200
    except Exception as e:
        print(f"Error unlinking dustbin for user {uid}: {e}")
        return jsonify({'error': 'Failed to unlink dustbin.'}), 500

@app.route('/leaderboard', methods=['GET'])
def leaderboard():
    try:
        query = db.collection('users').order_by('points.total', direction=firestore.Query.DESCENDING).limit(10)
        leaderboard_data = [{'email': user.to_dict().get('email', 'N/A'), 'total_points': user.to_dict().get('points', {}).get('total', 0)} for user in query.stream()]
        return jsonify(leaderboard_data)
    except Exception as e:
        return jsonify({'error': 'Could not load leaderboard.'}), 500

@app.route('/arduino_status', methods=['GET'])
def get_arduino_status():
    return jsonify(arduino_status)

@app.route('/ai_coach', methods=['POST'])
def ai_coach():
    if not model:
        return jsonify({'error': 'AI Coach is not configured on the server.'}), 500
        
    data = request.json
    try:
        points = data.get('user_data', {}).get('points', {})
        user_query = data.get('user_query', 'Give me some general advice.')

        prompt = f"""
        You are an encouraging and helpful AI sustainability coach for a Smart Dustbin app in Mangalagiri, Andhra Pradesh, India.
        Your goal is to provide useful, actionable advice based on the user's recycling data and their specific question.
        Keep your response concise (2-4 sentences) and friendly.

        Here is the user's current recycling data:
        - Total Points: {points.get('total', 0)}
        - Dry Waste Items Recycled: {points.get('dry', 0)}
        - Wet Waste Items Recycled: {points.get('wet', 0)}
        - E-Waste Items Recycled: {points.get('ewaste', 0)}

        Here is the user's question: "{user_query}"

        Directly answer the user's question. Only mention their points if their question is about points.
        """

        response = model.generate_content(prompt)
        return jsonify({'response': response.text})

    except Exception as e:
        print(f"🔥 AI Coach Error: {e}")
        return jsonify({'error': 'Failed to get a response from the AI coach.'}), 500

# --- Main Execution ---
if __name__ == '__main__':
    threading.Thread(target=read_from_arduino, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)