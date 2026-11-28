# OMEGA Voice - Persian Inverse Text Normalization (ITN)
# Converts spoken Persian numbers to integer digits (Persian/ASCII)

import re
from typing import List, Optional

U = {'صفر': 0, 'یک': 1, 'دو': 2, 'سه': 3, 'چهار': 4, 'پنج': 5, 'شش': 6, 'شیش': 6, 'هفت': 7, 'هشت': 8, 'نه': 9}
T = {'ده': 10, 'یازده': 11, 'دوازده': 12, 'سیزده': 13, 'چهارده': 14, 'پانزده': 15, 'پونزده': 15, 'شانزده': 16, 'شونزده': 16, 'هفده': 17, 'هیفده': 17, 'هجده': 18, 'هیجده': 18, 'نوزده': 19}
TY = {'بیست': 20, 'سی': 30, 'چهل': 40, 'پنجاه': 50, 'شصت': 60, 'هفتاد': 70, 'هشتاد': 80, 'نود': 90}
H = {'صد': 100, 'یکصد': 100, 'دویست': 200, 'سیصد': 300, 'چهارصد': 400, 'پانصد': 500, 'پونصد': 500, 'ششصد': 600, 'شیشصد': 600, 'هفتصد': 700, 'هشتصد': 800, 'نهصد': 900}
S = {'هزار': 1000, 'میلیون': 1000000, 'میلیارد': 1000000000}
NUM = set(U) | set(T) | set(TY) | set(H) | set(S)
_FA = '۰۱۲۳۴۵۶۷۸۹'


def _get_category(w: str) -> Optional[str]:
    if w in S:
        return 'S'
    if w in H:
        return 'H'
    if w in TY:
        return 'TY'
    if w in T:
        return 'T'
    if w in U:
        return 'U'
    return None


def _words_to_int(words: List[str]) -> int:
    total = 0
    current = 0
    for w in words:
        if w == 'و':
            continue
        if w in S:
            scale = S[w]
            if current == 0:
                current = 1
            total += current * scale
            current = 0
        elif w in H:
            current += H[w]
        elif w in TY:
            current += TY[w]
        elif w in T:
            current += T[w]
        elif w in U:
            current += U[w]
    return total + current


def _to_persian_digits(s: str) -> str:
    return ''.join(_FA[ord(c) - 48] if '0' <= c <= '9' else c for c in s)


def itn(text: str, persian_digits: bool = True) -> str:
    if not text:
        return ''

    parts = re.split(r'(\s+)', text)
    out = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if not token or token.isspace():
            out.append(token)
            i += 1
            continue

        m = re.match(r'^([^\w\s]*)([\w-]+)([^\w\s]*)$', token, flags=re.UNICODE)
        if not m:
            out.append(token)
            i += 1
            continue

        prefix, word, suffix = m.groups()
        if word not in NUM:
            out.append(token)
            i += 1
            continue

        seq_words = [word]
        last_cat = _get_category(word)
        last_scale = S.get(word, float('inf')) if last_cat == 'S' else float('inf')
        end_suffix = suffix

        j = i + 1
        if not suffix:
            while j < len(parts):
                if j < len(parts) and parts[j].isspace():
                    if j + 1 >= len(parts):
                        break
                    next_tok = parts[j + 1]
                    m_next = re.match(r'^([^\w\s]*)([\w-]+)([^\w\s]*)$', next_tok, flags=re.UNICODE)
                    if not m_next:
                        break
                    p_next, w_next, s_next = m_next.groups()
                    if p_next:
                        break

                    if w_next == 'و' and not s_next:
                        if j + 3 < len(parts) and parts[j + 2].isspace():
                            after_v_tok = parts[j + 3]
                            m_after = re.match(r'^([^\w\s]*)([\w-]+)([^\w\s]*)$', after_v_tok, flags=re.UNICODE)
                            if m_after and not m_after.group(1):
                                w_after = m_after.group(2)
                                s_after = m_after.group(3)
                                if w_after in NUM:
                                    cat_after = _get_category(w_after)
                                    scale_after = S.get(w_after, float('inf')) if cat_after == 'S' else None

                                    can_add = False
                                    if cat_after == 'S' and scale_after < last_scale:
                                        can_add = True
                                    elif cat_after != 'S':
                                        if last_cat == 'S':
                                            can_add = True
                                        elif last_cat == 'H' and cat_after in ('TY', 'T', 'U'):
                                            can_add = True
                                        elif last_cat == 'TY' and cat_after == 'U':
                                            can_add = True

                                    if can_add:
                                        seq_words.append('و')
                                        seq_words.append(w_after)
                                        last_cat = cat_after
                                        if cat_after == 'S':
                                            last_scale = scale_after
                                        end_suffix = s_after
                                        j += 4
                                        if s_after:
                                            break
                                        continue
                        break
                    elif w_next in NUM:
                        cat_next = _get_category(w_next)
                        scale_next = S.get(w_next, float('inf')) if cat_next == 'S' else None
                        can_add = False
                        if cat_next == 'S' and scale_next < last_scale:
                            can_add = True
                        elif cat_next != 'S':
                            if last_cat == 'S':
                                can_add = True
                            elif last_cat == 'H' and cat_next in ('TY', 'T', 'U'):
                                can_add = True
                            elif last_cat == 'TY' and cat_next == 'U':
                                can_add = True

                        if can_add:
                            seq_words.append(w_next)
                            last_cat = cat_next
                            if cat_next == 'S':
                                last_scale = scale_next
                            end_suffix = s_next
                            j += 2
                            if s_next:
                                break
                            continue
                        else:
                            break
                    else:
                        break
                else:
                    break

        val = _words_to_int(seq_words)
        val_str = str(val)
        out.append(prefix + val_str + end_suffix)
        i = j

    res = ''.join(out)
    if persian_digits:
        res = _to_persian_digits(res)
    return res


def apply_itn(text: str, persian_digits: bool = True) -> str:
    if not text:
        return ''
    try:
        return itn(text, persian_digits=persian_digits)
    except Exception:
        return text


if __name__ == '__main__':
    test_cases = [
        ('من بیست و پنج سال دارم.', 'من ۲۵ سال دارم.'),
        ('قیمت آن صد و بیست هزار تومان است.', 'قیمت آن ۱۲۰۰۰۰ تومان است.'),
        ('ساعت پنج و سی دقیقه است.', 'ساعت ۵ و ۳۰ دقیقه است.'),
        ('من دو سیب و سه پرتقال دارم', 'من ۲ سیب و ۳ پرتقال دارم'),
        ('یک میلیون و دویست هزار', '۱۲۰۰۰۰۰'),
        ('یکصد و بیست و سه هزار و چهارصد و پنجاه و شش', '۱۲۳۴۵۶'),
        ('شماره تلفن 1234 است', 'شماره تلفن ۱۲۳۴ است'),
        ('صفر', '۰'),
    ]
    for text_in, expected in test_cases:
        actual = apply_itn(text_in, persian_digits=True)
        assert actual == expected, f"Failed: '{text_in}' -> '{actual}' (expected '{expected}')"
    print('Persian ITN self-test PASSED (all cases verified)')
