import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { LocaleProvider } from '../../app/locale';
import { Timestamp } from './Workflow';

it('displays the server application calendar day and offset', () => {
  localStorage.clear();
  render(<LocaleProvider initial="en" timezone="Pacific/Kiritimati"><Timestamp value="2026-10-30T12:00:00Z" /></LocaleProvider>);
  const timestamp = screen.getByTitle(/GMT\+14:00/);
  expect(timestamp).toHaveTextContent('31');
  expect(timestamp).toHaveAttribute('datetime', '2026-10-30T12:00:00Z');
});
