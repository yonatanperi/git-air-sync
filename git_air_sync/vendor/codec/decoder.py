import os
import zipfile
import io
import xml.etree.ElementTree as ET

CHUNK_DELIMITER = "\n"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class Decoder:
    def __init__(self, input_path):
        self.input_path = input_path

    def decode(self):
        if os.path.isfile(self.input_path):
            self._decode_auto(self.input_path)
        elif os.path.isdir(self.input_path):
            self._decode_folder(self.input_path, self.input_path + "_decoded")
        else:
            print(f"{self.input_path} does not exist.")
            return

        print("Decoding process completed.")

    def _read_text(self, input_file):
        if input_file.endswith('.docx'):
            return self._docx_to_text(input_file)
        with open(input_file, 'r') as f:
            return f.read()

    def _docx_to_text(self, input_file):
        with zipfile.ZipFile(input_file, 'r') as zf:
            document_xml = zf.read('word/document.xml')

        root = ET.fromstring(document_xml)
        paragraphs = []
        for para in root.iter(f'{{{W_NS}}}p'):
            texts = [node.text or '' for node in para.iter(f'{{{W_NS}}}t')]
            paragraphs.append(''.join(texts))
        return CHUNK_DELIMITER.join(paragraphs)

    def _txt_to_bytes(self, input_file):
        content = self._read_text(input_file)

        chunks = content.split(CHUNK_DELIMITER)
        result = bytearray()
        for chunk in chunks:
            number_str = chunk.replace(' ', '')
            number = int(number_str)
            raw = number.to_bytes((number.bit_length() + 7) // 8, byteorder='big')
            result.extend(raw[1:])  # strip the \x01 prefix
        return bytes(result)

    def _decode_auto(self, input_file):
        raw_bytes = self._txt_to_bytes(input_file)
        if input_file.endswith('.txt'):
            output_name = input_file[:-len('.txt')]
        elif input_file.endswith('.docx'):
            output_name = input_file[:-len('.docx')]
        else:
            output_name = input_file + "_decoded"

        if zipfile.is_zipfile(io.BytesIO(raw_bytes)):
            with zipfile.ZipFile(io.BytesIO(raw_bytes), 'r') as zf:
                zf.extractall(output_name)
        else:
            with open(output_name, 'wb') as f:
                f.write(raw_bytes)

    def _decode_folder(self, input_folder, output_folder):
        for root, dirs, files in os.walk(input_folder):
            relative_root = os.path.relpath(root, input_folder)
            output_root = os.path.join(output_folder, relative_root)
            os.makedirs(output_root, exist_ok=True)

            for file in files:
                if file.endswith('.txt') or file.endswith('.docx'):
                    input_file_path = os.path.join(root, file)
                    self._decode_auto(input_file_path)
