import {request} from './client.js';

export const rewind = {
  listShifts: () => request('/rewind/shifts'),
  createShift: (body) => request('/rewind/shifts', {method: 'POST', body: JSON.stringify(body)}),
  getShift: (id) => request(`/rewind/shifts/${id}`),
  amendShift: (id, body) => request(`/rewind/shifts/${id}`, {method: 'PATCH', body: JSON.stringify(body)}),
  restore: (id) => request(`/rewind/shifts/${id}/restorations`, {method: 'POST', body: '{}'}),
  getRestoration: (shiftId, id) => request(`/rewind/shifts/${shiftId}/restorations/${id}`),
  adopt: (shiftId, id, expectedVersion) => request(
    `/rewind/shifts/${shiftId}/restorations/${id}/adopt`,
    {method: 'POST', body: JSON.stringify({expected_version: expectedVersion})}),
};
