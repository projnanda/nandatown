"""Strict, audited adapter around the pinned upstream PromptArmor implementation."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys
import time

from .web_evidence import EvidencePacket

PIN = '970d72286643f6faac7ceb8e647df7cbfffb221b'
PROVENANCE = ('source_sha256, start_offset, end_offset, validation=verbatim_match '
              'refer ONLY to the original webpage text, NOT the sanitized text. '
              'Sanitized integrity is recorded separately in promptarmor.json.')


class DefenseError(RuntimeError):
    def __init__(self, message, audit=None):
        super().__init__(message)
        self.audit = audit


def sha(text):
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def upstream():
    """Load only the four original upstream modules shipped in this wheel."""
    root = Path(__file__).parent / '_vendor' / 'promptarmor'
    identity = json.loads((root / 'SOURCE.json').read_text(encoding='utf-8'))
    if identity['commit'] != PIN:
        raise DefenseError('Unexpected vendored PromptArmor version')
    for relative, expected in identity['files_sha256_lf'].items():
        actual = (root / relative).read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(actual).hexdigest() != expected:
            raise DefenseError(f'Vendored upstream source changed: {relative}')
    modules = {name: importlib.import_module('nandatown._vendor.promptarmor.' + name)
               for name in ('config', 'prompts', 'detector', 'sanitize')}
    paths = {}
    for name, module in modules.items():
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(root.resolve()):
            raise DefenseError(f'Wrong upstream import: {path}')
        paths[name] = str(path)
    return modules, {**identity, 'imports': paths}


def strict_decision(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DefenseError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        obj = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError) as exc:
        raise DefenseError('Guard response is not one valid JSON object') from exc
    if (not isinstance(obj, dict) or set(obj) != {'injection', 'spans'}
            or type(obj['injection']) is not bool or not isinstance(obj['spans'], list)
            or any(not isinstance(s, str) or not s.strip() for s in obj['spans'])):
        raise DefenseError('Invalid guard decision schema')
    if obj['injection'] != bool(obj['spans']):
        raise DefenseError('Guard decision and spans disagree')
    return obj


def sanitize_with_audit(module, data, decision, cfg):
    """Observe real fuzzy_remove_one return frames; never implement deletion instead."""
    operations = []
    code = module.fuzzy_remove_one.__code__
    previous = sys.getprofile()
    def observe(frame, event, result):
        if previous:
            previous(frame, event, result)
        if event != 'return' or frame.f_code is not code:
            return
        values = frame.f_locals
        before, span = values['data'], values['span']
        operation = {'span': span, 'before': before, 'after': result}
        if result != before:
            # Capture upstream's actual regex match, or its actual exact-first branch.
            match = values.get('m')
            start, end = match.span() if match is not None else (before.find(span), before.find(span) + len(span))
            if start < 0 or before[:start] + before[end:] != result:
                operation['mapping_error'] = True
            else:
                operation.update(start=start, end=end)
        operations.append(operation)
    try:
        sys.setprofile(observe)
        cleaned = module.sanitize(data, decision, cfg)
    finally:
        sys.setprofile(previous)
    if cleaned['clean'] is None or len(operations) != len(decision['spans']):
        raise DefenseError('Unexpected upstream sanitizer execution')
    current = data
    indices = list(range(len(data)))
    deletions = []
    for ordinal, operation in enumerate(operations, 1):
        if operation['before'] != current or operation['after'] == current or operation.get('mapping_error'):
            raise DefenseError('Reported span was not successfully removed or cannot be mapped', audit={
                'candidate_cleaned_text': cleaned['clean'],
                'candidate_text_sha256': sha(cleaned['clean']),
                'upstream_removal_attempts': operations, 'validated_deletions': deletions,
                'candidate_is_not_authorized_for_delivery': True})
        start, end = operation['start'], operation['end']
        selected = indices[start:end]
        # Map to original text; an upstream match may bridge earlier deleted ranges.
        ranges = []
        for index in selected:
            if ranges and ranges[-1][1] == index:
                ranges[-1][1] = index + 1
            else:
                ranges.append([index, index + 1])
        deletions.append({'order': ordinal, 'requested_span': operation['span'],
                          'current_range': [start, end], 'original_ranges': ranges,
                          'removed_text': current[start:end],
                          'original_segments': [{'range': [a, b], 'text': data[a:b]} for a, b in ranges]})
        indices = indices[:start] + indices[end:]
        current = operation['after']
    kept = set(indices)
    reconstructed = ''.join(c for i, c in enumerate(data) if i in kept)
    if current != cleaned['clean'] or reconstructed != current:
        raise DefenseError('Deletion reconstruction failed')
    return current, deletions


class Guard:
    def __init__(self, client, log, report_path, per_text=120.0, total=600.0):
        self.modules, self.identity = upstream()
        self.client, self.log, self.report_path = client, log, Path(report_path)
        self.per_text, self.total = per_text, total
        self.cfg = self.modules['config'].PAConfig(on_detect='remove', removal_length_guard=3.0,
                    include_definition=False, model=client.model, temperature=0, use_guided_json=False)
        self.detector = self.modules['detector'].Detector(self.cfg, client)
        self.report = {'upstream': self.identity, 'provenance': PROVENANCE,
                       'offset_unit': 'Python Unicode code-point indices; half-open ranges',
                       'fields': [], 'status': 'pending'}
        self.persist()

    def persist(self):
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')

    def packet(self, packet: EvidencePacket):
        started = time.monotonic()
        fields = [(f'items[{i}].text', item.text) for i, item in enumerate(packet.items)]
        fields.append(('completion_note', packet.completion_note))
        outputs = []
        try:
            for field, original in fields:
                entry = {'field': field, 'original_text': original, 'original_text_sha256': sha(original),
                         'cleaned_text': None, 'cleaned_text_sha256': None, 'deletions': []}
                self.report['fields'].append(entry)
                tick = time.monotonic()
                if original:
                    remaining = self.total - (tick - started)
                    if remaining <= 0:
                        raise DefenseError('Guard total timeout')
                    self.client.timeout_seconds = min(self.per_text, remaining)
                    self.client.field = field
                    decision = self.detector.detect(original, user_task=packet.question)
                    entry.update(raw_response=decision['raw'], upstream_decision=decision)
                    strict = strict_decision(decision['raw'])
                    if strict['injection'] != decision['injection'] or strict['spans'] != decision['spans']:
                        raise DefenseError('Upstream and strict decisions disagree')
                    if time.monotonic() - tick >= self.client.timeout_seconds:
                        raise DefenseError('Guard field timeout')
                    cleaned, deletions = sanitize_with_audit(self.modules['sanitize'], original, decision, self.cfg)
                else:
                    cleaned, deletions = original, []
                    entry['skipped_empty'] = True
                if time.monotonic() - started >= self.total:
                    raise DefenseError('Guard total timeout')
                entry.update(cleaned_text=cleaned, cleaned_text_sha256=sha(cleaned), deletions=deletions,
                             elapsed_seconds=time.monotonic()-tick)
                self.log.emit('guard', 'field_sanitized', {'field': field, 'deletions': deletions,
                              'original_text_sha256': sha(original), 'cleaned_text_sha256': sha(cleaned)})
                outputs.append(cleaned)
                self.persist()
            result = packet.model_copy(update={
                'items': [item.model_copy(update={'text': outputs[i]}) for i, item in enumerate(packet.items)],
                'completion_note': outputs[-1]})
            self.report.update(status='completed', elapsed_seconds=time.monotonic()-started)
            self.persist()
            return result
        except Exception as exc:
            if isinstance(exc, DefenseError) and exc.audit and self.report['fields']:
                self.report['fields'][-1]['failed_sanitization_audit'] = exc.audit
            self.report.update(status='defense_error', error_type=type(exc).__name__,
                               error='Guard did not complete; inspect raw decision/events',
                               elapsed_seconds=time.monotonic()-started)
            self.persist()
            self.log.emit('guard', 'defense_error', {'type': type(exc).__name__})
            raise DefenseError('PromptArmor failed; Agent 2 must not run') from exc
