# Parse only supported single-line dotenv bindings; never execute their contents.
# One parser supplies validation, effective emptiness and replacement decisions.
function invalid() {
    print "Unsupported dotenv syntax; existing environment preserved." > "/dev/stderr"
    failed = 1
    exit 1
}
function binding(line, text, rest, end, quote) {
    parsed_key = ""
    parsed_rhs = ""
    bare_comment = ""
    text = line
    sub(/\r$/, "", text)
    sub(/^[ \t]*/, "", text)
    if (text == "" || substr(text, 1, 1) == "#") return
    sub(/^export[ \t]+/, "", text)
    quote = sprintf("%c", 39)
    if (substr(text, 1, 1) == quote) {
        end = index(substr(text, 2), quote)
        if (!end) invalid()
        parsed_key = substr(text, 2, end - 1)
        rest = substr(text, end + 2)
    } else {
        if (!match(text, /^[^= \t#]+/)) invalid()
        parsed_key = substr(text, 1, RLENGTH)
        rest = substr(text, RLENGTH + 1)
    }
    sub(/^[ \t]*/, "", rest)
    if (rest == "" || substr(rest, 1, 1) == "#") {
        bare_comment = rest
        return
    }
    if (substr(rest, 1, 1) != "=") invalid()
    parsed_rhs = substr(rest, 2)
    sub(/^[ \t]*/, "", parsed_rhs)
}
function value_state(value, quote, i, char, next_char, tail) {
    parsed_comment = ""
    quote = substr(value, 1, 1)
    if (quote == "\"" || quote == sprintf("%c", 39)) {
        for (i = 2; i <= length(value); i++) {
            char = substr(value, i, 1)
            next_char = substr(value, i + 1, 1)
            if (char == "\\" && (next_char == quote || next_char == "\\")) {
                i++
                continue
            }
            if (char == quote) {
                tail = substr(value, i + 1)
                sub(/^[ \t]*/, "", tail)
                if (tail != "" && substr(tail, 1, 1) != "#") invalid()
                parsed_comment = tail
                return i == 2 ? "empty" : "configured"
            }
        }
        invalid()  # Multiline/unterminated quotes cannot be rewritten safely.
    }
    if (match(value, /[ \t]+#/)) {
        parsed_comment = substr(value, RSTART)
        sub(/^[ \t]*/, "", parsed_comment)
        value = substr(value, 1, RSTART - 1)
    }
    sub(/[ \t]*$/, "", value)
    return value == "" ? "empty" : "configured"
}
BEGIN {
    count = split(keys, required, " ")
    for (i = 1; i <= count; i++) protected[required[i]] = 1
    if (action == "replace") {
        generated = ENVIRON["AURUMAI_GENERATED_SECRET"]
        if (generated !~ /^[a-zA-Z0-9._+:/@=-]+$/) {
            quote = sprintf("%c", 39)
            gsub(/\\/, "\\\\", generated)
            gsub(quote, "\\" quote, generated)
            generated = quote generated quote
        }
    }
}
{
    binding($0)
    state = value_state(parsed_rhs)
    if (bare_comment != "") parsed_comment = bare_comment
    if (action == "validate" && parsed_key in protected && ++seen[parsed_key] > 1) {
        print "Duplicate required environment assignment; existing environment preserved." > "/dev/stderr"
        failed = 1
        exit 1
    }
    if (action == "state" && parsed_key == key) result = state
    if (action == "replace") {
        if (parsed_key == key) {
            print key "=" generated (parsed_comment == "" ? "" : " " parsed_comment)
            found = 1
        } else print $0
    }
}
END {
    if (!failed && action == "state") print result == "" ? "empty" : result
    if (!failed && action == "replace" && !found) print key "=" generated
}
