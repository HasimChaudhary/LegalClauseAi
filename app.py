import os
# AGGRESSIVE FIX: Strip all proxy settings BEFORE any other imports
# This prevents the 'Client.__init__ proxies' error in Google GenAI SDK
for var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
    os.environ.pop(var, None)

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, flash, Response, stream_with_context, session
from flask_pymongo import PyMongo
from flask_login import LoginManager, UserMixin, login_user, current_user, login_required, logout_user
from flask_bcrypt import Bcrypt
from bson import ObjectId
import markdown
import json
import feedparser
import requests
import re

from parser.file_reader import read_file
from parser.simplifier import simplify_text, simplify_text_stream
from parser.chat_engine import chat_with_gemini_stream, chat_with_groq_stream, get_constitution_text
import datetime

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

# Production-ready MongoDB connection
mongo_uri = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI")
if not mongo_uri:
    raise RuntimeError("No MongoDB URI found. Set MONGO_URI in .env")

# Ensure the database name "legalclause" is included in the URI
if "mongodb.net" in mongo_uri and "/legalclause" not in mongo_uri:
    if "?" in mongo_uri:
        mongo_uri = mongo_uri.replace(".net/", ".net/legalclause")
        if ".net/?" in mongo_uri:
            mongo_uri = mongo_uri.replace(".net/?", ".net/legalclause?")
    else:
        mongo_uri = mongo_uri.rstrip("/") + "/legalclause"

app.config["MONGO_URI"] = mongo_uri
app.config["MONGO_CONNECT_TIMEOUT_MS"] = 30000
app.config["MONGO_SERVER_SELECTION_TIMEOUT_MS"] = 30000

mongo = PyMongo(app)
bcrypt = Bcrypt(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

class User(UserMixin):
    def __init__(self, doc):
        self.id = str(doc["_id"])
        self.email = doc.get("email")

@login_manager.user_loader
def load_user(user_id):
    try:
        doc = mongo.db.users.find_one({"_id": ObjectId(user_id)})
        return User(doc) if doc else None
    except Exception:
        return None

WHITELIST = {"login", "register", "static", "favicon"}

@app.before_request
def require_login_for_all():
    endpoint = (request.endpoint or "").split(".")[-1]
    if endpoint in WHITELIST or (request.endpoint or "").startswith("static"):
        return
    if current_user.is_authenticated:
        return
    return redirect(url_for("login", next=request.path))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Please provide email and password", "warning")
            return redirect(url_for("register"))
        if mongo.db.users.find_one({"email": email}):
            flash("Email already registered", "danger")
            return redirect(url_for("register"))
        hashpw = bcrypt.generate_password_hash(password).decode()
        mongo.db.users.insert_one({"email": email, "password": hashpw})
        flash("Registered! Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user_doc = mongo.db.users.find_one({"email": email})
        if user_doc and bcrypt.check_password_hash(user_doc["password"], password):
            user_obj = User(user_doc)
            login_user(user_obj)
            flash("Logged in successfully.", "success")
            next_page = request.form.get("next") or request.args.get("next")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("home"))
        flash("Invalid email or password", "danger")
        return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def home():
    return render_template("home.html")

@app.route("/upload", methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        file = request.files.get('file')
        text_input = request.form.get('text', '')
        text = ""
        
        if file:
            try:
                text = read_file(file)
                if text.startswith("Error:"):
                    flash(text, "danger")
                    return redirect(url_for('upload'))
            except Exception as e:
                flash(f"Error processing document: {str(e)}", "danger")
                return redirect(url_for('upload'))
        elif text_input:
            text = text_input
        else:
            flash("No file or text provided", "warning")
            return redirect(url_for('upload'))
        
        return render_template('result.html', original_text=text)
        
    return render_template('upload.html')

@app.route('/stream_analysis', methods=['POST'])
@login_required
def stream_analysis():
    data = request.get_json()
    text = data.get('text')
    
    if not text:
        return Response("No text provided", status=400)

    def generate():
        for chunk in simplify_text_stream(text):
            yield chunk

    return Response(stream_with_context(generate()), mimetype='text/plain')

from parser.chat_engine import chat_with_gemini_stream

@app.route('/chat')
@login_required
def chat():
    return render_template('chat.html')

@app.route('/chat_api', methods=['POST'])
@login_required
def chat_api():
    try:
        data = request.get_json()
        message = data.get('message')
        history = data.get('history', [])
        
        if not message:
            return Response("No message provided", status=400)

        def generate():
            try:
                for chunk in chat_with_gemini_stream(message, history):
                    yield chunk
            except Exception as e:
                print(f"Error in chat stream: {e}")
                yield f"Error: {str(e)}"

        return Response(stream_with_context(generate()), mimetype='text/plain')
    except Exception as e:
        print(f"Error in chat_api: {e}")
        return Response(str(e), status=500)

@app.route('/news')
@login_required
def news():
    return render_template('news.html')

@app.route('/learning')
@login_required
def learning():
    return render_template('learning.html')

@app.route('/learning/law')
@login_required
def learning_law():
    track_progress('law')
    return render_template('learning_law.html')

@app.route('/learning/law/<law_name>')
@login_required
def learning_law_view(law_name):
    # Sample data for Articles/Sections
    data = {
        'Constitution of India': [
            {'id': 'Article 12', 'title': 'Definition of the State'},
            {'id': 'Article 14', 'title': 'Equality before law'},
            {'id': 'Article 15', 'title': 'Prohibition of discrimination on grounds of religion, race, caste, sex or place of birth'},
            {'id': 'Article 19', 'title': 'Protection of certain rights regarding freedom of speech'},
            {'id': 'Article 21', 'title': 'Protection of life and personal liberty'},
            {'id': 'Article 32', 'title': 'Remedies for enforcement of rights conferred by this Part'},
            {'id': 'Article 44', 'title': 'Uniform civil code for the citizens'},
            {'id': 'Article 51A', 'title': 'Fundamental Duties'}
        ],
        'IPC': [
            {'id': 'Section 124A', 'title': 'Sedition'},
            {'id': 'Section 299', 'title': 'Culpable homicide'},
            {'id': 'Section 300', 'title': 'Murder'},
            {'id': 'Section 354', 'title': 'Assault or criminal force to woman with intent to outrage her modesty'},
            {'id': 'Section 378', 'title': 'Theft'},
            {'id': 'Section 390', 'title': 'Robbery'},
            {'id': 'Section 420', 'title': 'Cheating and dishonestly inducing delivery of property'},
            {'id': 'Section 498A', 'title': 'Husband or relative of husband of a woman subjecting her to cruelty'}
        ],
        'CrPC': [
            {'id': 'Section 41', 'title': 'When police may arrest without warrant'},
            {'id': 'Section 46', 'title': 'How arrest made'},
            {'id': 'Section 144', 'title': 'Power to issue order in urgent cases of nuisance or apprehended danger'},
            {'id': 'Section 154', 'title': 'Information in cognizable cases (FIR)'},
            {'id': 'Section 164', 'title': 'Recording of confessions and statements'},
            {'id': 'Section 167', 'title': 'Procedure when investigation cannot be completed in twenty-four hours'},
            {'id': 'Section 438', 'title': 'Direction for grant of bail to person apprehending arrest'}
        ],
        'Contract Act': [
            {'id': 'Section 2', 'title': 'Interpretation-clause'},
            {'id': 'Section 10', 'title': 'What agreements are contracts'},
            {'id': 'Section 11', 'title': 'Who are competent to contract'},
            {'id': 'Section 23', 'title': 'What considerations and objects are lawful, and what not'},
            {'id': 'Section 73', 'title': 'Compensation for loss or damage caused by breach of contract'},
            {'id': 'Section 124', 'title': '"Contract of indemnity" defined'}
        ],
        'RTI Act': [
            {'id': 'Section 3', 'title': 'Right to Information'},
            {'id': 'Section 4', 'title': 'Obligations of public authorities'},
            {'id': 'Section 6', 'title': 'Request for obtaining information'},
            {'id': 'Section 8', 'title': 'Exemption from disclosure of information'},
            {'id': 'Section 19', 'title': 'Appeal'},
            {'id': 'Section 20', 'title': 'Penalties'}
        ]
    }
    items = data.get(law_name, [])
    return render_template('learning_law_view.html', law_name=law_name, items=items)

@app.route('/learning/law/<law_name>/<item_id>')
@login_required
def learning_content(law_name, item_id):
    # In a real app, we'd fetch the original text from a DB or PDF.
    # Here we use sample text or AI to generate it if missing.
    sample_texts = {
        'Article 14': "The State shall not deny to any person equality before the law or the equal protection of the laws within the territory of India.",
        'Article 21': "No person shall be deprived of his life or personal liberty except according to procedure established by law.",
        'Section 378': "Whoever, intending to take dishonestly any moveable property out of the possession of any person without that person's consent, moves that property in order to such taking, is said to commit theft."
    }
    
    original_text = sample_texts.get(item_id, f"Original legal text for {item_id} in {law_name}...")
    
    # Use AI to simplify and generate example/MCQ
    prompt = f"""
    Law: {law_name}
    Clause: {item_id}
    Text: {original_text}

    Provide:
    1. A very simple explanation for a student.
    2. A real-life example.
    3. One MCQ with 4 options and the correct answer.

    Format the response as JSON:
    {{
        "explanation": "...",
        "example": "...",
        "mcq": {{
            "question": "...",
            "options": ["...", "...", "...", "..."],
            "answer": "..."
        }}
    }}
    """
    
    try:
        # Using a non-streaming helper for this specific task
        response_text = "".join(chat_with_groq_stream(prompt, system_instruction="You are a legal educator. Return ONLY JSON."))
        # Basic JSON extraction in case AI adds markdown
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            ai_data = json.loads(json_match.group())
        else:
            raise ValueError("No JSON found")
            
        return render_template('learning_content.html', 
                             law_name=law_name, 
                             item_id=item_id, 
                             original_text=original_text,
                             simplified_explanation=ai_data['explanation'],
                             example=ai_data['example'],
                             mcq=ai_data['mcq'])
    except Exception as e:
        print(f"Error generating learning content: {e}")
        return "Error loading content. Please try again later.", 500

@app.route('/learning/case')
@login_required
def learning_case():
    track_progress('case')
    # Sample scenario
    scenario = "Rahul was walking home at night when a police officer stopped him and arrested him without telling him the reason for the arrest. Rahul was not allowed to call his lawyer or family for 24 hours."
    return render_template('learning_case.html', scenario=scenario)

@app.route('/api/learning/evaluate-case', methods=['POST'])
@login_required
def evaluate_case():
    data = request.json
    prompt = f"""
    Scenario: {data['scenario']}
    User's Answer (Clause): {data['user_clause']}
    User's Reasoning: {data['user_reasoning']}

    Evaluate the user's answer.
    Provide:
    1. The correct legal clause (Article/Section).
    2. A short reasoning.
    3. A simple explanation.

    Format as JSON:
    {{
        "correct_clause": "...",
        "reasoning": "...",
        "explanation": "..."
    }}
    """
    try:
        response_text = "".join(chat_with_groq_stream(prompt, system_instruction="You are a legal evaluator. Return ONLY JSON."))
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        return Response(json_match.group(), mimetype='application/json')
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route('/learning/exam')
@login_required
def learning_exam():
    track_progress('exam')
    return render_template('learning_exam.html')

@app.route('/api/learning/generate-exam-answer', methods=['POST'])
@login_required
def generate_exam_answer():
    data = request.json
    prompt = f"""
    Law: {data['law']}
    Topic: {data['topic']}
    Marks: {data['marks']}

    Generate a structured exam-style answer.
    Use headings like:
    - Introduction
    - Relevant Legal Provisions
    - Key Points / Explanation
    - Case Laws (if any)
    - Conclusion

    Keep the language simple but professional.
    """
    try:
        answer = "".join(chat_with_groq_stream(prompt, system_instruction="You are a law professor helping a student. Use markdown for headings."))
        return json.dumps({"answer": answer})
    except Exception as e:
        return json.dumps({"error": str(e)}), 500

@app.route('/learning/daily')
@login_required
def learning_daily():
    track_progress('daily')
    
    concepts = [
        {
            "title": "Right to Information",
            "law": "RTI Act, 2005",
            "clause": "Section 3",
            "explanation": "Every citizen has the right to request information from a public authority.",
            "example": "You can file an RTI to know the status of road repairs in your locality."
        },
        {
            "title": "Presumption of Innocence",
            "law": "Indian Evidence Act",
            "clause": "Section 101",
            "explanation": "A person is considered innocent until proven guilty in a court of law.",
            "example": "The burden of proof lies on the prosecution to prove the accused committed the crime."
        },
        {
            "title": "Bail as a Right",
            "law": "CrPC",
            "clause": "Section 436",
            "explanation": "In bailable offenses, bail is a matter of right for the accused.",
            "example": "If arrested for a minor traffic violation, you are entitled to bail immediately."
        },
        {
            "title": "Freedom of Speech and Expression",
            "law": "Constitution of India",
            "clause": "Article 19(1)(a)",
            "explanation": "Every citizen has the right to express their views freely, subject to reasonable restrictions.",
            "example": "You can write a blog post criticizing a government policy without fear of arrest, provided it doesn't incite violence."
        },
        {
            "title": "Right to Equality",
            "law": "Constitution of India",
            "clause": "Article 14",
            "explanation": "The State shall not deny anyone equality before the law or equal protection of the laws.",
            "example": "A government job cannot be denied to someone legally qualified solely based on their religion or caste."
        },
        {
            "title": "Right to Life and Personal Liberty",
            "law": "Constitution of India",
            "clause": "Article 21",
            "explanation": "No person shall be deprived of their life or personal liberty except according to procedure established by law.",
            "example": "You have the right to a clean environment and prompt medical treatment in government hospitals."
        },
        {
            "title": "Protection against Double Jeopardy",
            "law": "Constitution of India",
            "clause": "Article 20(2)",
            "explanation": "No person shall be prosecuted and punished for the same offense more than once.",
            "example": "If you have already served a sentence for a specific theft, you cannot be tried again for the exact same theft."
        },
        {
            "title": "Right against Self-Incrimination",
            "law": "Constitution of India",
            "clause": "Article 20(3)",
            "explanation": "No person accused of an offense shall be compelled to be a witness against themselves.",
            "example": "The police cannot force you to confess to a crime using physical or mental torture (the right to remain silent)."
        },
        {
            "title": "First Information Report (FIR)",
            "law": "CrPC",
            "clause": "Section 154",
            "explanation": "The police must record your complaint if the information discloses the commission of a cognizable (serious) offense.",
            "example": "If someone snatches your phone, the police are legally bound to register an FIR."
        },
        {
            "title": "Right to Legal Aid",
            "law": "Constitution of India",
            "clause": "Article 39A",
            "explanation": "The State must provide free legal aid to ensure that justice is not denied due to economic or other disabilities.",
            "example": "If you cannot afford a lawyer to defend yourself in court, the state will appoint a public defender for you free of cost."
        },
        {
            "title": "Writ of Habeas Corpus",
            "law": "Constitution of India",
            "clause": "Article 32",
            "explanation": "A legal order demanding that a person detained by the state be brought before a court to determine if their detention is lawful.",
            "example": "If a friend is illegally detained by the police without charges, a court can order their immediate release via this writ."
        },
        {
            "title": "Criminal Intimidation",
            "law": "IPC",
            "clause": "Section 503",
            "explanation": "Threatening another person with injury to their person, reputation, or property to cause alarm or force them to do an act.",
            "example": "Sending messages threatening to harm someone if they don't withdraw a complaint against you is a crime."
        },
        {
            "title": "Defamation",
            "law": "IPC",
            "clause": "Section 499",
            "explanation": "Making or publishing any false imputation concerning any person, intending to harm their reputation.",
            "example": "Publishing false allegations in a newspaper claiming a local business owner is a fraud can lead to defamation charges."
        },
        {
            "title": "Right to Privacy",
            "law": "Constitution of India",
            "clause": "Article 21 (Puttaswamy Judgment)",
            "explanation": "Privacy is recognized as a fundamental right, intrinsic to the right to life and liberty.",
            "example": "The government cannot secretly monitor your emails or tap your phone without a lawful and necessary purpose."
        },
        {
            "title": "Cheating",
            "law": "IPC",
            "clause": "Section 415",
            "explanation": "Deceiving someone fraudulently or dishonestly to deliver property or to agree to do something they wouldn't have done otherwise.",
            "example": "Selling a fake watch online by claiming it is an original, expensive brand constitutes cheating."
        }
    ]
    
    # Rotate based on day of year
    day_of_year = datetime.datetime.now().timetuple().tm_yday
    concept = concepts[day_of_year % len(concepts)]
    
    return render_template('learning_daily.html', concept=concept, date=datetime.datetime.now().strftime("%B %d, %Y"))

@app.route('/learning/progress')
@login_required
def learning_progress():
    progress = session.get('learning_progress', {})
    return render_template('learning_progress.html', progress=progress)

def track_progress(mode):
    if 'learning_progress' not in session:
        session['learning_progress'] = {}
    
    progress = session['learning_progress']
    if mode not in progress:
        progress[mode] = 0
    progress[mode] += 1
    session['learning_progress'] = progress
    session.modified = True




@app.route('/api/news')
@login_required
def get_news():
    category = request.args.get('category', 'national')
    rss_urls = {
        'national': 'https://www.thehindu.com/news/national/feeder/default.rss',
        'international': 'https://www.thehindu.com/news/international/feeder/default.rss',
        'business': 'https://www.thehindu.com/business/feeder/default.rss',
        'sport': 'https://www.thehindu.com/sport/feeder/default.rss',
        'entertainment': 'https://www.thehindu.com/entertainment/feeder/default.rss',
        'science': 'https://www.thehindu.com/sci-tech/science/feeder/default.rss'
    }

    
    url = rss_urls.get(category, rss_urls['national'])
    
    try:
        feed = feedparser.parse(url)
        news_items = []
        for entry in feed.entries:
            # Extract image if available
            image_url = None
            if 'media_content' in entry:
                image_url = entry.media_content[0]['url']
            elif 'links' in entry:
                for link in entry.links:
                    if 'image' in link.get('type', ''):
                        image_url = link.get('href')
                        break
            
            # Fallback for image in description or summary
            if not image_url and 'summary' in entry:
                import re
                img_match = re.search(r'<img src="([^"]+)"', entry.summary)
                if img_match:
                    image_url = img_match.group(1)

            news_items.append({
                'title': entry.title,
                'link': entry.link,
                'description': entry.summary if 'summary' in entry else '',
                'published': entry.published if 'published' in entry else '',
                'image': image_url
            })
        return json.dumps(news_items)
    except Exception as e:
        return json.dumps({'error': str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, use_reloader=True)
