export type CreationOptions = Omit<PublicKeyCredentialCreationOptions, 'challenge' | 'user' | 'excludeCredentials'> & {
  challenge: string;
  user: Omit<PublicKeyCredentialUserEntity, 'id'> & { id: string };
  excludeCredentials?: Array<Omit<PublicKeyCredentialDescriptor, 'id'> & { id: string }>;
};

export function decodeBase64Url(value: string): ArrayBuffer {
  const normalized = value.replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')), c => c.charCodeAt(0)).buffer;
}

export function encodeBase64Url(value: ArrayBuffer): string {
  return btoa(Array.from(new Uint8Array(value), byte => String.fromCharCode(byte)).join('')).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function registrationOptions(options: CreationOptions): PublicKeyCredentialCreationOptions {
  return { ...options, challenge: decodeBase64Url(options.challenge), user: { ...options.user, id: decodeBase64Url(options.user.id) }, excludeCredentials: options.excludeCredentials?.map(item => ({ ...item, id: decodeBase64Url(item.id) })) };
}

export function registrationPayload(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAttestationResponse;
  return { id: credential.id, rawId: encodeBase64Url(credential.rawId), type: credential.type, response: { attestationObject: encodeBase64Url(response.attestationObject), clientDataJSON: encodeBase64Url(response.clientDataJSON), transports: response.getTransports?.() ?? [] } };
}

export type AuthenticationOptions = Omit<PublicKeyCredentialRequestOptions, 'challenge' | 'allowCredentials'> & { challenge: string; allowCredentials?: Array<Omit<PublicKeyCredentialDescriptor, 'id'> & { id: string }> };

export function authenticationOptions(options: AuthenticationOptions): PublicKeyCredentialRequestOptions {
  return { ...options, challenge: decodeBase64Url(options.challenge), allowCredentials: options.allowCredentials?.map(item => ({ ...item, id: decodeBase64Url(item.id) })) };
}

export function authenticationPayload(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAssertionResponse;
  return { id: credential.id, rawId: encodeBase64Url(credential.rawId), type: credential.type, response: { authenticatorData: encodeBase64Url(response.authenticatorData), clientDataJSON: encodeBase64Url(response.clientDataJSON), signature: encodeBase64Url(response.signature), userHandle: response.userHandle ? encodeBase64Url(response.userHandle) : null } };
}
