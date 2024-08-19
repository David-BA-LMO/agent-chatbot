from fastapi import FastAPI, Depends, HTTPException, Response
from uuid import uuid4, UUID
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from session_manager import SessionData, MongoDBBackend, CustomSessionVerifier, CookieBackend
from src.router_chain import Router_chain
import os
from dotenv import load_dotenv
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler


# ------CLASE DE INICIO------
class MainApp:

    #------CONSTRUCTOR------
    def __init__(self):
        # Inicializar FastAPI
        self.app = FastAPI()
        # Ruta a la base de datos de MongoDB
        load_dotenv()
        self.MONGO_URI = os.getenv("MONGO_URI")
        if self.MONGO_URI is None or self.MONGO_URI == "":
            print("MONGO_URI is not founded")
            exit(1)
        self.client = AsyncIOMotorClient(self.MONGO_URI) # Motor de MongoDB para operaciones CRUD asincrónicas
        
        self.db = self.client.db # Base de datos de MongoDB. El nombre está definido en docker-compose.yml 
        self.sessions_collection = self.db.sessions # Colección para almacenar las sesiones de usuario
        self.session_router_chain = {}  # Aquí se guardarán las instancias de lógica del chatbot

        # Crear instancias del backend y frontend
        self.mongo_backend = MongoDBBackend(self.sessions_collection) #Backend de sesión
        self.cookie_backend = CookieBackend(cookie_name="session_id", secret_key=os.getenv("SECRET_KEY", "supersecretkey"), backend=self.mongo_backend) # Backend de cookies
        self.session_verifier = CustomSessionVerifier(backend=self.mongo_backend ) # Verificador de sesión

        # Se define el tiempo de espera de la sesión en minutos
        session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT"))
        self.session_timeout = timedelta(minutes=session_timeout_minutes)

        self.app.mount("/static", StaticFiles(directory="static"), name="static") # Se monta el directorio de archivos estáticos

        # Configuración del logger con rotación (limitados). 20000 bytes y 5 archivos de respaldo máximos
        handler = RotatingFileHandler('app.log', maxBytes=20000, backupCount=5)
        logging.basicConfig(handlers=[handler], level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        logger = logging.getLogger()

        # Registrar rutas
        self._register_routes()


    #------REGISTRO DE RUTAS------
    def _register_routes(self):
        @self.app.get("/", response_class=HTMLResponse)
        async def read_root():
            return self.get_html_response()
        
        @self.app.post("/start_session")
        async def start_session(response: Response):
            return await self._start_session(response)

        @self.app.post("/chat")
        async def chat(response: Response, user_input: str, session_id: UUID = Depends(self.cookie_backend.read)):
            if not await self.session_verifier.verify_session(str(session_id)): # Verificación de sesión
                raise HTTPException(status_code=404, detail="Session not found or expired")
            return await self._chat(response, user_input, str(session_id)) # Interacción con el chatbot

        @self.app.post("/end_session")
        async def end_session(response: Response, session_id: UUID = Depends(self.cookie_backend.read)):
            return await self._end_session(response, str(session_id))


    #------RESPUESTA HTML------ 
    def get_html_response(self):
        index_path = Path("templates/index.html")
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        else:
            return HTMLResponse(content="<h1>404: File Not Found</h1>", status_code=404)


    # -----INICIO DE SESIÓN------
    async def _start_session(self, response: Response):
        # Crear una nueva sesión con un ID único
        session_id = uuid4()
        current_time = datetime.now(timezone.utc) # Se obtiene la hora actual
        session_data = SessionData(
            session_id=str(session_id), # se crea una sesión con un ID único
            data={"conversation": []},  # data es un diccionario. Iniciamos un elemento "conversation" que es una lista (la cual almacenará diccionarios)
            last_active=datetime.now(timezone.utc), # Establece el instante de inicio de la sesión
            expiration_time=current_time + self.session_timeout # Establece el tiempo de expiración de la sesión
        )
        session_logic = Router_chain() # Crear la lógica de negocio para la sesión
        self.session_router_chain[str(session_id)] = session_logic
        await self.mongo_backend.create(str(session_id), session_data) # Creamos la sesión en la base de datos
        self.cookie_backend.write(response, str(session_id)) # Esta función utiliza el objeto Response para adjuntar la cookie al encabezado HTTP enviada al cliente
        return {"session_id": str(session_id)}


    #------MANEJO DE INTERACCIÓNES USUARIO-CHATBOT------
    # Utiliza la cookie del cliente para identificar la sesión
    async def _chat(self, response: Response, user_input: str, session_id: UUID):
        # Verificar si la sesión existe en la colección de sesiones del backend de MongoDB
        session = await self.mongo_backend.read(str(session_id)) # Se obtiene la sesión con el ID proporcionado
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        # Actualizar la sesión con la nueva conversación
        # Verificar si la sesión ha expirado
        if datetime.now(timezone.utc) > session.expiration_time:
            await self.mongo_backend.delete(str(session_id))
            self.cookie_backend.delete(response)
            raise HTTPException(status_code=401, detail="Session expired")
        
        # Recuperar la instancia de Router_chain, que contiene las cadenas de enrutamiento del chatbot
        router_chain = self.session_router_chain.get(str(session_id))
        if not router_chain:
            raise HTTPException(status_code=500, detail="Session logic not found")
        chatbot_response = router_chain.execute_chatbot(user_input) # EJECUCIÓN DEL CHATBOT
        
        conversation_entry = {"user": user_input, "bot": chatbot_response, "timestamp": datetime.now(timezone.utc)}
        # Dentro del objeto de datos de la sesión, se añade la conversación. Cada entrada es un diccionario con el mensaje del usuario, la respuesta del chatbot y la marca de tiempo
        session.data["conversation"].append(conversation_entry)
        # Se actualiza el instante de la última interacción
        session.last_active = datetime.now(timezone.utc)
        session.expiration_time = session.last_active + self.session_timeout
        await self.mongo_backend.update(str(session_id), session)

        # Enviar la respuesta del chatbot al cliente
        return {"response": chatbot_response}


    #------CIERRE DE SESIÓN------  
    async def _end_session(self, response: Response, session_id: UUID):
        # Terminar la sesión actual
        await self.mongo_backend.delete(str(session_id))
        self.cookie_backend.delete(response)
        self.session_router_chain.pop(str(session_id), None)
        return {"detail": "Session ended"}

# Iniciar la aplicación
main_app = MainApp()
app = main_app.app
