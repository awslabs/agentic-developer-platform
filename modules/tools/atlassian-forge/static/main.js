import { invokeRemote } from '@forge/bridge';
import { pair } from './pair.mjs';
import './style.css';

const form = document.querySelector('#pair-form');
const input = document.querySelector('#pair-code');
const status = document.querySelector('#status');
form.addEventListener('submit', async event => {
  event.preventDefault();
  const code = input.value.trim();
  input.value = '';
  form.querySelector('button').disabled = true;
  status.textContent = 'Verifying the connection…';
  try {
    await pair(invokeRemote, code);
    status.textContent = 'Connected. Return to ADP and choose your Jira projects in Product connections.';
  } catch {
    status.textContent = 'Could not confirm the connection. Refresh its status in ADP before trying a new code.';
  } finally {
    form.querySelector('button').disabled = false;
  }
});
