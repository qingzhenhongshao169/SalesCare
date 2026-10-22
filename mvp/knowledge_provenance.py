"""Canonical build identities. Similarity and hashes are not proof of business truth."""
import hashlib
import json
import math


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def vector_digest(vector):
    return digest(json.dumps(vector, separators=(',', ':'), allow_nan=False))


def build_manifest(tid, version, pieces, fingerprint, mode, parser):
    if not pieces:
        raise ValueError('empty_index')
    sizes = {len(c.vector) for c in pieces}
    if len(sizes) != 1:
        raise ValueError('vector_dimension_changed')
    dimension = next(iter(sizes))
    if (mode == 'keyword' and dimension != 0) or (mode != 'keyword' and not 8 <= dimension <= 8192):
        raise ValueError('vector_format')
    for c in pieces:
        if c.digest != digest(c.content):
            raise ValueError('chunk_integrity')
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in c.vector):
            raise ValueError('vector_format')
        if c.vector_digest and c.vector_digest != vector_digest(c.vector):
            raise ValueError('vector_integrity')
    summary = [[c.position, c.digest, vector_digest(c.vector)] for c in pieces]
    data = {'schema': 'salescare.build.v1', 'tenant_id': tid, 'version_id': version.id,
            'source_digest': version.digest, 'parser': parser,
            'chunker': 'native-360-heading-path-v2' if parser=='native-v2' else 'native-360-v1',
            'embedding_fingerprint': fingerprint, 'embedding_mode': mode, 'dimension': dimension,
            'chunk_count': len(pieces), 'chunks_digest': digest(json.dumps(summary, separators=(',', ':')))}
    data['generation'] = digest(json.dumps(data, sort_keys=True))
    return data


def evidence_summary(record):
    """Persist identifiers and hashes, never whole documents or provider credentials."""
    return {'id': record['id'], 'version': record['version'], **record.get('evidence', {})}
