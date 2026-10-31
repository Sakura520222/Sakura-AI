import { describe, expect, it } from 'vitest';
import { translate } from './translate';

describe('localized dynamic values', () => {
  it('keeps zero and explicit false values when formatting a label', () => {
    expect(translate('数量 {0}，开关 {1}', 'Count {0}, enabled {1}', 'en', [0, false])).toBe('Count 0, enabled false');
  });
  it('does not reinterpret placeholders contained in user or provider text', () => {
    expect(translate('设备 {0}', 'Device {0}', 'en', ['key {1}'])).toBe('Device key {1}');
  });
});
