import os
import uuid
import PyPDF2
from flask import Flask, request, jsonify, render_template, session
import google.generativeai as genai
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

DEMO_MODE = True 

# SQLite Database Setup
basedir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(basedir, 'data')
if not os.path.exists(data_dir):
    os.makedirs(data_dir)

db_path = os.path.join(data_dir, 'chat_history.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database Models
class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

with app.app_context():
    db.create_all()

# Configure Gemini
api_key = os.getenv("GEMINI_API_KEY")
if api_key and not DEMO_MODE:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

# In-memory document context
doc_contexts = {}

def get_token_count(text):
    """Calculates tokens using the Gemini API."""
    if DEMO_MODE:
        return len(text) // 4
    if not api_key or not text:
        return 0
    try:
        return model.count_tokens(text).total_tokens
    except:
        # Fallback to rough estimation (4 chars per token) if API call fails
        return len(text) // 4

def extract_text(file):
    filename = file.filename.lower()
    text = ""
    try:
        if filename.endswith('.pdf'):
            reader = PyPDF2.PdfReader(file)
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted: text += extracted + "\n"
        elif filename.endswith('.txt') or filename.endswith('.csv'):
            text = file.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"Extraction error: {e}")
    return text

@app.route('/')
def index():
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    return render_template('index.html', demo_mode=DEMO_MODE)

@app.route('/new-session', methods=['POST'])
def new_session():
    """Wipes current session data to start a fresh chat."""
    old_session_id = session.get('session_id')
    if old_session_id in doc_contexts:
        del doc_contexts[old_session_id]
    
    session.clear()
    session['session_id'] = str(uuid.uuid4())
    return jsonify({'status': 'success'})

@app.route('/tokens', methods=['GET'])
def get_tokens():
    """Calculates the total tokens currently in context (Files + History)."""
    session_id = session.get('session_id')
    total = 0
    
    # Count tokens in uploaded documents
    if session_id in doc_contexts:
        total += get_token_count("".join(doc_contexts[session_id]))
        
    # Count tokens in last 10 messages of history
    history = ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.timestamp.desc()).limit(10).all()
    history_text = "".join([f"{m.role}: {m.content}" for m in history])
    total += get_token_count(history_text)
    
    return jsonify({'total_tokens': total})

@app.route('/history', methods=['GET'])
def get_history():
    session_id = session.get('session_id')
    messages = ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.timestamp.asc()).all()
    return jsonify([{
        'role': m.role,
        'content': m.content
    } for m in messages])

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    session_id = session.get('session_id')
    
    text = extract_text(file)
    if not text.strip():
         return jsonify({'error': 'Empty file'}), 400

    if session_id not in doc_contexts:
         doc_contexts[session_id] = []
    
    doc_contexts[session_id].append(f"Document ({file.filename}):\n{text}\n")
    return jsonify({'message': f'File {file.filename} added.'})

@app.route('/clear-context', methods=['POST'])
def clear_context():
    """Removes all uploaded files from the current session context."""
    session_id = session.get('session_id')
    if session_id in doc_contexts:
        doc_contexts[session_id] = []
    return jsonify({'message': 'Context cleared.'})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '')
    session_id = session.get('session_id')
    
    # Save user message
    new_user_msg = ChatMessage(session_id=session_id, role='user', content=user_message)
    db.session.add(new_user_msg)
    db.session.commit()

    if DEMO_MODE:
        bot_response = "Demo only: Input received, no calls were made to the LLM. In production this will be a real response from the LLM."
    else:
        if not api_key:
            return jsonify({'response': 'API Key missing'}), 500

        history_messages = ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.timestamp.asc()).all()
        
        full_prompt = "System: Use the file context and history to answer accurately.\n"
        if session_id in doc_contexts:
            full_prompt += "FILE CONTEXT:\n" + "".join(doc_contexts[session_id]) + "\n"
        
        for msg in history_messages[-10:]:
            full_prompt += f"{msg.role.upper()}: {msg.content}\n"
        
        full_prompt += f"USER: {user_message}\nASSISTANT:"

        try:
            response = model.generate_content(full_prompt)
            bot_response = response.text

        except Exception as e:
            return jsonify({'response': f"Error: {str(e)}"}), 500
        
    # Save bot response
    new_bot_msg = ChatMessage(session_id=session_id, role='bot', content=bot_response)
    db.session.add(new_bot_msg)
    db.session.commit()
    return jsonify({'response': bot_response})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)