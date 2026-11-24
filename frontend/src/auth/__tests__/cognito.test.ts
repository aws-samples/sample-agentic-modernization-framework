import { describe, expect, it } from 'vitest';

import { cognitoOrigin, isAuthCode, toAuthCode } from '../cognito';

describe('cognitoOrigin', () => {
  it('accepts the Hosted UI domain and keeps only its origin', () => {
    expect(cognitoOrigin('https://amf-portal.auth.us-east-1.amazoncognito.com')).toBe(
      'https://amf-portal.auth.us-east-1.amazoncognito.com',
    );
    expect(cognitoOrigin('https://amf-portal.auth.us-east-1.amazoncognito.com/x?y=1')).toBe(
      'https://amf-portal.auth.us-east-1.amazoncognito.com',
    );
  });

  it.each([
    undefined,
    '',
    'not a url',
    'http://amf-portal.auth.us-east-1.amazoncognito.com',
    'https://evil.example.com',
    'https://amf-portal.auth.us-east-1.amazoncognito.com.evil.example.com',
    'https://user@evil.example.com#.auth.us-east-1.amazoncognito.com',
    'https://a.b.auth.us-east-1.amazoncognito.com',
    'https://amf_portal.auth.us-east-1.amazoncognito.com',
    'https://amf-portal.auth.us-east-1.amazoncognito.com:8443',
  ])('rejects %s', (domain) => {
    expect(cognitoOrigin(domain)).toBeNull();
  });
});

describe('isAuthCode', () => {
  it('accepts a Cognito authorization code', () => {
    expect(isAuthCode('3f2a9c1e-7b4d-4e8a-9f0c-1d2e3f4a5b6c')).toBe(true);
  });
  it.each(['', 'abc/def', 'x?y=1', 'a b', 'https://evil.example.com', 'a'.repeat(257)])('rejects %s', (code) => {
    expect(isAuthCode(code)).toBe(false);
  });
});

describe('toAuthCode', () => {
  it('returns an equal copy of a valid code and null otherwise', () => {
    expect(toAuthCode('3f2a9c1e-7b4d')).toBe('3f2a9c1e-7b4d');
    expect(toAuthCode(null)).toBeNull();
    expect(toAuthCode('a'.repeat(100_000) + '!')).toBeNull();
  });
});
