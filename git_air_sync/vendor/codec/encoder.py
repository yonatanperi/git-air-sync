import os
import re
import zipfile
import io

CHUNK_SIZE = 1024
CHUNK_DELIMITER = "\n"


class Encoder:
    def __init__(self, input_path):
        self.input_path = input_path

    def encode(self):
        if os.path.isfile(self.input_path):
            with open(self.input_path, 'rb') as f:
                raw_bytes = f.read()
        elif os.path.isdir(self.input_path):
            raw_bytes = self._zip_folder(self.input_path)
        else:
            print(f"{self.input_path} does not exist.")
            return

        text = self._encode_text(raw_bytes)
        with open(self.input_path + ".txt", 'w') as f:
            f.write(text)
        self._write_docx(text, self.input_path + ".docx")
        print("Encoding process completed.")

    def _encode_text(self, raw_bytes):
        chunks = []
        for i in range(0, len(raw_bytes), CHUNK_SIZE):
            chunk = raw_bytes[i:i + CHUNK_SIZE]
            chunks.append(self._format_chunk(chunk))
        return CHUNK_DELIMITER.join(chunks)

    def _write_docx(self, text, output_file):
        paragraphs = "".join(
            "<w:p><w:r><w:t xml:space=\"preserve\">{}</w:t></w:r></w:p>".format(
                self._escape_xml(line)
            )
            for line in text.split(CHUNK_DELIMITER)
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main">'
            '<w:body>{}</w:body></w:document>'.format(paragraphs)
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types">'
            '<Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main'
            '+xml"/></Types>'
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        )
        with zipfile.ZipFile(output_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', content_types)
            zf.writestr('_rels/.rels', rels)
            zf.writestr('word/document.xml', document_xml)

    def _escape_xml(self, text):
        return (
            text.replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
        )

    def _format_chunk(self, chunk):
        number = int.from_bytes(b'\x01' + chunk, byteorder='big')
        number_str = str(number)
        formatted = ""
        for i in range(0, len(number_str), 5):
            formatted += number_str[i:i + 5] + ' '
        return formatted.strip()

    def _zip_folder(self, folder_path):
        gitignore_rules = self._load_gitignore_rules(folder_path)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(folder_path):
                rel_root = os.path.relpath(root, folder_path)

                def rel(name):
                    return name if rel_root == "." else os.path.join(rel_root, name)

                dirs[:] = [
                    d for d in dirs
                    if d != ".git"
                    and not self._is_gitignored(gitignore_rules, rel(d), is_dir=True)
                ]
                for file in files:
                    if self._is_gitignored(gitignore_rules, rel(file), is_dir=False):
                        continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, folder_path)
                    zf.write(file_path, arcname)
        return buffer.getvalue()

    def _load_gitignore_rules(self, root_dir):
        rules = []
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if ".gitignore" not in filenames:
                continue
            base_dir = os.path.relpath(dirpath, root_dir).replace(os.sep, "/")
            if base_dir == ".":
                base_dir = ""
            gitignore_path = os.path.join(dirpath, ".gitignore")
            with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.rstrip("\r\n").strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    rules.append(self._compile_gitignore_rule(stripped, base_dir))
        return rules

    def _compile_gitignore_rule(self, pattern, base_dir):
        negate = pattern.startswith("!")
        if negate:
            pattern = pattern[1:]
        dir_only = pattern.endswith("/")
        if dir_only:
            pattern = pattern[:-1]
        anchored = pattern.startswith("/")
        if anchored:
            pattern = pattern[1:]
        elif "/" in pattern:
            anchored = True

        body = self._glob_to_regex(pattern)
        prefix = re.escape(base_dir) + "/" if base_dir else ""
        if anchored:
            regex = re.compile("^" + prefix + body + "$")
        else:
            regex = re.compile("^" + prefix + "(?:.*/)?" + body + "$")

        return {"regex": regex, "negate": negate, "dir_only": dir_only}

    def _glob_to_regex(self, pattern):
        i, n = 0, len(pattern)
        out = []
        while i < n:
            c = pattern[i]
            if c == "*":
                if pattern[i:i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                elif pattern[i:i + 2] == "**":
                    out.append(".*")
                    i += 2
                else:
                    out.append("[^/]*")
                    i += 1
            elif c == "?":
                out.append("[^/]")
                i += 1
            elif c == "[":
                j = i + 1
                if j < n and pattern[j] == "!":
                    j += 1
                if j < n and pattern[j] == "]":
                    j += 1
                while j < n and pattern[j] != "]":
                    j += 1
                if j >= n:
                    out.append(re.escape(c))
                    i += 1
                else:
                    content = pattern[i + 1:j]
                    if content.startswith("!"):
                        content = "^" + content[1:]
                    out.append("[" + content + "]")
                    i = j + 1
            else:
                out.append(re.escape(c))
                i += 1
        return "".join(out)

    def _is_gitignored(self, rules, rel_path, is_dir):
        rel_path = rel_path.replace(os.sep, "/")
        ignored = False
        for rule in rules:
            if rule["dir_only"] and not is_dir:
                continue
            if rule["regex"].match(rel_path):
                ignored = not rule["negate"]
        return ignored
