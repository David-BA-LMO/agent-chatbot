import re

# Formatea la respuesta final dada por el chatbot en la búsqueda de base de datos
async def qa_format_text(text):
        # Dividir el texto por "++++"
        parts = text.split("++++")
        first_separator = True
        
        for part in parts:
            if first_separator:
                #  Añadir un salto de línea antes de la primera parte
                part = '\n\n' + part
                first_separator = False

            part = re.sub(r'(?<=[.:])(?! )', ' ', part)

            # Separar URLs
            urls = re.findall(r'http\S+', part)
            text_without_urls = re.sub(r'http\S+', '', part)

            # Devolver el texto sin URLs
            if text_without_urls.strip():
                yield text_without_urls.strip()

            # Devolver las URLs
            for url in urls:
                yield f"{url} "