import google.generativeai as genai
from flask import Flask, request, jsonify, render_template
import requests
import os
import fitz
import sched
import time
import logging
from mimetypes import guess_type
from datetime import datetime,timedelta
from urlextract import URLExtract
from training import instructions, product_images
from sqlalchemy import create_engine, Column, Integer, String, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

logging.basicConfig(level=logging.INFO)

#Set this 'True' if you want to get the chat history as a database of {phone no: chats} format.
#But you need to set up vercel postgres database and set the 'DB_URL' environment variable with the database url.
#Otherwise the bot will crash.
db=False
wa_token=os.environ.get("WA_TOKEN") # Whatsapp API Key
gen_api=os.environ.get("GEN_API") # Gemini API Key
owner_phone=os.environ.get("OWNER_PHONE") # Owner's phone number with countrycode
model_name="gemini-2.0-flash"

app = Flask(__name__)
genai.configure(api_key=gen_api)

class CustomURLExtract(URLExtract):
    def _get_cache_file_path(self):
        cache_dir = "/tmp"
        return os.path.join(cache_dir, "tlds-alpha-by-domain.txt")

extractor = CustomURLExtract(limit=1)

generation_config = {
  "temperature": 1,
  "top_p": 0.95,
  "top_k": 0,
  "max_output_tokens": 8192,
}

safety_settings = [
  {"category": "HARM_CATEGORY_HARASSMENT","threshold": "BLOCK_MEDIUM_AND_ABOVE"},
  {"category": "HARM_CATEGORY_HATE_SPEECH","threshold": "BLOCK_MEDIUM_AND_ABOVE"},  
  {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT","threshold": "BLOCK_MEDIUM_AND_ABOVE"},
  {"category": "HARM_CATEGORY_DANGEROUS_CONTENT","threshold": "BLOCK_MEDIUM_AND_ABOVE"},
]

model = genai.GenerativeModel(model_name=model_name,
                              generation_config=generation_config,
                              safety_settings=safety_settings)

convo = model.start_chat(history=[])
convo.send_message(instructions.instructions)


def send(answer,sender,phone_id):
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {
        'Authorization': f'Bearer {wa_token}',
        'Content-Type': 'application/json'
    }
    type="text"
    body="body"
    content=answer
    image_urls=product_images.image_urls
    if "product_image" in answer:
        for product in image_urls.keys():
            if product in answer:
                answer=answer.replace("product_image",image_urls[product])
                urls=extractor.find_urls(answer)
                if len(urls)>0:
                    mime_type,_=guess_type(urls[0].split("/")[-1])
                    type=mime_type.split("/")[0]
                    body="link"
                    content=urls[0]
                    answer=answer.replace(urls[0],"\n")
                    break
    data = {
        "messaging_product": "whatsapp",
        "to": sender,
        "type": type,
        type: {
            body:content,
            **({"caption":answer} if type!="text" else {})
            },
        }
    response = requests.post(url, headers=headers, json=data)
    if db:
        insert_chat("Bot",answer)
    return response

def remove(*file_paths):
    for file in file_paths:
        if os.path.exists(file):
            os.remove(file)
        else:pass

if db:
    db_url=os.environ.get("DB_URL") # Database URL
    engine=create_engine(db_url)
    Session=sessionmaker(bind=engine)
    Base=declarative_base()
    scheduler = sched.scheduler(time.time, time.sleep)
    report_time = datetime.now().replace(hour=22, minute=00, second=0, microsecond=0)

    class Chat(Base):
        __tablename__ = 'chats'
        Chat_no = Column(Integer, primary_key=True)
        Sender = Column(String(255), nullable=False)
        Message = Column(String, nullable=False)
        Chat_time = Column(DateTime, default=datetime.utcnow)

    logging.info("Creating tables if they do not exist...")
    Base.metadata.create_all(engine)

    def insert_chat(sender, message):
        logging.info("Inserting chat into database")
        try:
            session = Session()
            chat = Chat(Sender=sender, Message=message)
            session.add(chat)
            session.commit()
            logging.info("Chat inserted successfully")
        except Exception as e:
            logging.error(f"Error inserting chat: {e}")
            session.rollback()
        finally:
            session.close()

    def get_chats(sender):
        try:
            session = Session()
            chats = session.query(Chat.Message).filter(Chat.Sender == sender).all()
            return chats
        except:pass
        finally:
            session.close()

    def delete_old_chats():
        try:
            session = Session()
            cutoff_date = datetime.now() - timedelta(days=14)
            session.query(Chat).filter(Chat.Chat_time < cutoff_date).delete()
            session.commit()
            logging.info("Old chats deleted successfully")
        except:
            session.rollback()
        finally:
            session.close()

    def create_report(phone_id):
        logging.info("Creating report")
        try:
            today = datetime.today().strftime('%d-%m-%Y')
            session = Session()
            query = session.query(Chat.Message).filter(func.date_trunc('day', Chat.Chat_time) == today).all()
            if query:
                chats = '\n\n'.join(query)
                send(chats, owner_phone, phone_id)
        except Exception as e:
            logging.error(f"Error creating report: {e}")
        finally:
            session.close()
            
else:pass

def message_handler(data,phone_id):
    sender=data["from"]
    if data["type"] == "text":
        prompt = data["text"]["body"]
        if db:insert_chat(owner_phone,prompt)
        convo.send_message(prompt)
    else:
        media_url_endpoint = f'https://graph.facebook.com/v19.0/{data[data["type"]]["id"]}/'
        headers = {'Authorization': f'Bearer {wa_token}'}
        media_response = requests.get(media_url_endpoint, headers=headers)
        media_url = media_response.json()["url"]
        media_download_response = requests.get(media_url, headers=headers)
        if data["type"] == "audio":
            filename = "/tmp/temp_audio.mp3"
        elif data["type"] == "image":
            filename = "/tmp/temp_image.jpg"
        elif data["type"] == "document":
            doc=fitz.open(stream=media_download_response.content,filetype="pdf")
            for _,page in enumerate(doc):
                destination="/tmp/temp_image.jpg"
                pix = page.get_pixmap()
                pix.save(destination)
                file = genai.upload_file(path=destination,display_name="tempfile")
                response = model.generate_content(["Read this document carefully and explain it in detail",file])
                answer=response._result.candidates[0].content.parts[0].text
                convo.send_message(f'''Direct image input has limitations,
                                    so this message is created by an llm model based on the document send by the user, 
                                    reply to the customer assuming you saw that document.
                                    (Warn the customer and stop the chat if it is not related to the business): {answer}''')
                remove(destination)
        else:send("This format is not Supported by the bot ☹",sender,phone_id)
        if data["type"] == "image" or data["type"] == "audio":
            with open(filename, "wb") as temp_media:
                temp_media.write(media_download_response.content)
            file = genai.upload_file(path=filename,display_name="tempfile")
            if data["type"] == "image":
                response = model.generate_content(["What is in this image?",file])
                answer=response.text
                convo.send_message(f'''Customer has sent an image,
                                    So here is the llm's reply based on the image sent by the customer:{answer}\n\n''')
                urls=extractor.find_urls(convo.last.text)
                if len(urls)>0:
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
                    response=requests.get(urls[0],headers=headers)
                    img_path="/tmp/prod_image.jpg"
                    with open(img_path, "wb") as temp_media:
                        temp_media.write(response.content)
                    img=genai.upload_file(path=img_path,display_name="prodfile")
                    response=model.generate_content(["Is the things in both the images are exactly same? Explain in detail",img,file])
                    answer=response.text
                    convo.send_message(f'''This is the message from AI after comparing the two images: {answer}''')
            else:
                response = model.generate_content(["What is the content of this audio file?",file])
                answer=response.text
                convo.send_message(f'''Direct media input has limitations,
                                            so this message is created by an llm model based on the audio send by the user, 
                                            reply to the customer assuming you heard that audio.
                                            (Warn the customer and stop the chat if it is not related to the business): {answer}''')
            remove("/tmp/temp_image.jpg","/tmp/temp_audio.mp3","/tmp/prod_image.jpg")
        files=genai.list_files()
        for file in files:
            file.delete()
    reply=convo.last.text
    if "unable_to_solve_query" in reply:
        send(f"customer {sender} is not satisfied", owner_phone, phone_id)
        reply=reply.replace("unable_to_solve_query",'\n')
        send(reply, sender, phone_id)
    else:send(reply,sender,phone_id)
    if db:
        scheduler.enterabs(report_time.timestamp(), 1, create_report, (phone_id,))
        scheduler.run(blocking=False)
        delete_old_chats()
        
@app.route("/", methods=["GET", "POST"])
def index():
    return render_template("connected.html")

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == "BOT":
            return challenge, 200
        else:
            return "Failed", 403
    elif request.method == "POST":
        try:
            data = request.get_json()["entry"][0]["changes"][0]["value"]["messages"][0]
            phone_id=request.get_json()["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
            message_handler(data,phone_id)
        except :pass
        return jsonify({"status": "ok"}), 200
if __name__ == "__main__":
    app.run(debug=True, port=8000)