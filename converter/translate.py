"""Snowflake semantic-view expressions → Sigma formulas.

Identifiers are resolved by the caller. A warehouse function the formula language
does not have becomes CallNumber / CallText / CallDatetime, and the caller records that.
"""

import re


class TranslateError(Exception):
    pass


_FUNCS = {
    "SUM": "Sum",
    "AVG": "Avg",
    "MIN": "Min",
    "MAX": "Max",
    "COUNT": "Count",
    "ROUND": "Round",
    "NULLIF": "NullIf",
    "COALESCE": "Coalesce",
    "NVL": "Coalesce",
    "UPPER": "Upper",
    "LOWER": "Lower",
    "ABS": "Abs",
    "FLOOR": "Floor",
    "CEIL": "Ceil",
    "LENGTH": "Length",
    "TRIM": "Trim",
}


class Parser:
    def __init__(self, source, resolve):
        self.s = source
        self.n = len(source)
        self.i = 0
        self.resolve = resolve
        self.calls = []

    def parse(self):
        expr = self._expr()
        self._skip()
        if self.i < self.n:
            raise TranslateError("could not read the rest of the expression: " + self.s[self.i : self.i + 60])
        return expr

    def _skip(self):
        while self.i < self.n and self.s[self.i].isspace():
            self.i += 1

    def _peek(self):
        self._skip()
        return self.s[self.i] if self.i < self.n else ""

    def _starts(self, word):
        self._skip()
        end = self.i + len(word)
        if self.s[self.i : end].upper() != word.upper():
            return False
        nxt = self.s[end] if end < self.n else ""
        if word[0].isalnum() and (nxt.isalnum() or nxt == "_"):
            return False
        return True

    def _eat(self, word):
        if not self._starts(word):
            return False
        self.i += len(word)
        return True

    def _expect(self, ch):
        if self._peek() != ch:
            raise TranslateError(f"expected {ch} in {self.s[max(0, self.i - 20): self.i + 20]!r}")
        self.i += 1

    def _ident(self):
        self._skip()
        m = re.match(r"[A-Za-z_][\w$]*", self.s[self.i :])
        if not m:
            return None
        self.i += m.end()
        return m.group(0)

    def _path(self):
        first = self._ident()
        if not first:
            return None
        parts = [first]
        while True:
            self._skip()
            if self.i < self.n and self.s[self.i] == ".":
                nxt = self.i + 1
                while nxt < self.n and self.s[nxt].isspace():
                    nxt += 1
                if nxt < self.n and (self.s[nxt].isalpha() or self.s[nxt] == "_"):
                    self.i = nxt
                    part = self._ident()
                    if not part:
                        break
                    parts.append(part)
                    continue
            break
        return parts

    def _quoted_ident(self):
        """Snowflake double-quoted identifier, which may contain spaces."""
        self._expect('"')
        chars = []
        while self.i < self.n:
            ch = self.s[self.i]
            if ch == '"' and self.i + 1 < self.n and self.s[self.i + 1] == '"':
                chars.append('"')
                self.i += 2
                continue
            if ch == '"':
                self.i += 1
                return self.resolve(["".join(chars)])
            chars.append(ch)
            self.i += 1
        raise TranslateError("unterminated identifier")

    def _string(self):
        self._expect("'")
        chars = []
        while self.i < self.n:
            ch = self.s[self.i]
            if ch == "'" and self.i + 1 < self.n and self.s[self.i + 1] == "'":
                chars.append("'")
                self.i += 2
                continue
            if ch == "'":
                self.i += 1
                return '"' + "".join(chars).replace('"', '""') + '"'
            chars.append(ch)
            self.i += 1
        raise TranslateError("unterminated string")

    def _number(self):
        m = re.match(r"\d+(?:\.\d+)?", self.s[self.i :])
        self.i += m.end()
        return m.group(0)

    def _expr(self):
        return self._or()

    def _or(self):
        left = self._and()
        while self._eat("OR"):
            left = f"{left} or {self._and()}"
        return left

    def _and(self):
        left = self._not()
        while self._eat("AND"):
            left = f"{left} and {self._not()}"
        return left

    def _not(self):
        if self._eat("NOT"):
            return f"Not({self._not()})"
        return self._cmp()

    def _cmp(self):
        left = self._add()
        if self._eat("IS"):
            neg = self._eat("NOT")
            if not self._eat("NULL"):
                raise TranslateError("expected NULL after IS")
            # A comparison with null never matches in Sigma. IsNull / IsNotNull do.
            return f"IsNotNull({left})" if neg else f"IsNull({left})"
        ops = ("<>", "!=", "<=", ">=", "=", "<", ">")
        self._skip()
        for op in ops:
            if self.s.startswith(op, self.i):
                self.i += len(op)
                sigma = "!=" if op == "<>" else op
                return f"{left} {sigma} {self._add()}"
        if self._eat("NOT"):
            if not self._eat("IN"):
                raise TranslateError("expected IN after NOT")
            return f"Not({self._in(left)})"
        if self._eat("IN"):
            return self._in(left)
        return left

    def _in(self, left):
        self._expect("(")
        items = [self._expr()]
        while self._peek() == ",":
            self.i += 1
            items.append(self._expr())
        self._expect(")")
        return f"In({left}, {', '.join(items)})"

    def _add(self):
        left = self._mul()
        while True:
            ch = self._peek()
            if ch in ("+", "-"):
                # A leading sign belongs to the next term only when we already have a left side.
                self.i += 1
                left = f"{left} {ch} {self._mul()}"
            elif self.s.startswith("||", self.i):
                self.i += 2
                left = f'CallText("CONCAT", {left}, {self._mul()})'
            else:
                break
        return left

    def _mul(self):
        left = self._unary()
        while self._peek() in ("*", "/"):
            op = self.s[self.i]
            self.i += 1
            left = f"{left} {op} {self._unary()}"
        return left

    def _unary(self):
        if self._peek() == "-":
            self.i += 1
            return f"-{self._unary()}"
        return self._primary()

    def _primary(self):
        if self._eat("CASE"):
            return self._case()
        if self._eat("NULL"):
            return "null"
        if self._eat("TRUE"):
            return "true"
        if self._eat("FALSE"):
            return "false"
        if self._peek() == "'":
            return self._string()
        if self._peek() == '"':
            return self._quoted_ident()
        if self._peek().isdigit():
            return self._number()
        if self._peek() == "(":
            self.i += 1
            expr = self._expr()
            self._expect(")")
            return f"({expr})"
        path = self._path()
        if not path:
            raise TranslateError("expected a value at " + self.s[self.i : self.i + 40])
        if self._peek() == "(":
            return self._call(path)
        return self.resolve(path)

    def _case(self):
        arms = []
        else_expr = "null"
        while self._eat("WHEN"):
            cond = self._expr()
            if not self._eat("THEN"):
                raise TranslateError("CASE is missing THEN")
            arms.append((cond, self._expr()))
        if self._eat("ELSE"):
            else_expr = self._expr()
        if not self._eat("END"):
            raise TranslateError("CASE is missing END")
        if not arms:
            raise TranslateError("CASE has no WHEN")
        flat = []
        for cond, value in arms:
            flat.extend((cond, value))
        flat.append(else_expr)
        return "If(" + ", ".join(flat) + ")"

    def _call(self, parts):
        name = ".".join(parts)
        upper = name.upper()
        self._expect("(")
        distinct = self._eat("DISTINCT")
        if self._peek() == ")":
            args = []
        elif self._peek() == "*":
            self.i += 1
            args = ["*"]
        else:
            args = [self._expr()]
            while self._peek() == ",":
                self.i += 1
                args.append(self._expr())
        self._expect(")")
        self.calls.append(upper)
        if upper == "IFF" or upper == "IF":
            return "If(" + ", ".join(args) + ")"
        if upper == "COUNT_IF":
            return "CountIf(" + ", ".join(args) + ")"
        if upper == "COUNT" and distinct:
            return "CountDistinct(" + ", ".join(args) + ")"
        if upper == "COUNT" and args == ["*"]:
            return "Count()"
        if upper == "DATEDIFF" and len(args) == 3:
            unit = args[0]
            if len(unit) >= 2 and unit[0] == '"' and unit[-1] == '"':
                unit = '"' + unit[1:-1].lower() + '"'
            return f"DateDiff({unit}, {args[1]}, {args[2]})"
        if upper == "DATE_TRUNC":
            part = args[0]
            if part.startswith('"') and part.endswith('"'):
                part = '"' + part[1:-1].lower() + '"'
            rest = ", ".join(args[1:])
            return f"DateTrunc({part}, {rest})" if rest else f"DateTrunc({part})"
        if upper == "CURRENT_DATE":
            return 'CallDatetime("CURRENT_DATE")'
        if upper == "CURRENT_TIMESTAMP":
            return "Now()"
        if upper == "CONCAT":
            return 'CallText("CONCAT", ' + ", ".join(args) + ")"
        if upper in _FUNCS:
            fn = _FUNCS[upper]
            if upper == "COUNT" and not args:
                return "Count()"
            return fn + "(" + ", ".join(args) + ")"
        # Scalar warehouse function. Numeric context is the common one for metrics.
        self.calls.append("WAREHOUSE:" + name)
        return 'CallNumber("' + name + '", ' + ", ".join(args) + ")"


def translate(source, resolve):
    parser = Parser(source, resolve)
    return parser.parse(), parser.calls
