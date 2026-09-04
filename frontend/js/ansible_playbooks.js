/**
 * ansible_playbooks.js — Where the Playbooks area actually lives (#586).
 *
 * It is in `ansible.js`, not here. That file was the Ansible side panel
 * before the view existed: the runner, both playbook lists, the editor, the
 * run dialog and the live event stream, all working and all tested. When
 * the panel became an area, the sensible move was to rehost the markup and
 * leave the code alone rather than reimplement 1,300 working lines in a new
 * idiom for no behavioural gain.
 *
 * So this file exists only to say so. Every other area has a script named
 * after it, and a missing `ansible_playbooks.js` would send the next person
 * looking for one — or, worse, tempt them to write one, which would then
 * register the same area name and quietly win or lose depending on script
 * order.
 *
 * `ansible.js` calls `ansibleView.area('playbooks', …)` on DOMContentLoaded
 * and listens for `shellmate:ansible-refresh`.
 */
