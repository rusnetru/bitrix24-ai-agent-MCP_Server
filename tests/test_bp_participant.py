from dataclasses import replace

import pytest

from b24agent.agent import Agent
from b24agent.bp_participant import BPParticipant, coerce, parse_document, task_fields
from b24agent.entities import EntityRegistry
from b24agent.imbot import ImBotV2
from b24agent.store import Store
from b24agent.tools import build_toolbox
from test_agent import BOT_ID, FakeBitrix, FakeLLM
from test_agent import settings  # noqa: F401  (фикстура)

APPROVE_TASK = {
    'ID': '41', 'WORKFLOW_ID': 'wf1', 'DOCUMENT_ID': ['crm', 'CCrmDocumentDeal', 'DEAL_12'],
    'DOCUMENT_NAME': 'Сделка: Поставка', 'DOCUMENT_URL': '/crm/deal/details/12/', 'NAME': 'Проверить договор',
    'DESCRIPTION': 'Проверь договор во вложении: оплата не позже 30 дней.', 'ACTIVITY': 'ApproveActivity',
    'WORKFLOW_TEMPLATE_NAME': 'Согласование договора',
}
INFO_TASK = {
    'ID': '42', 'WORKFLOW_ID': 'wf2', 'DOCUMENT_ID': ['crm', 'Bitrix\\Crm\\Integration\\BizProc\\Document\\Dynamic',
                                                      'DYNAMIC_1038_5'],
    'DOCUMENT_NAME': 'Проект', 'NAME': 'Оценить риски', 'DESCRIPTION': 'Оцени риск проекта',
    'ACTIVITY': 'RequestInformationActivity',
    'PARAMETERS': {'Fields': [
        {'Id': 'risk', 'Type': 'select', 'Name': 'Риск', 'Required': '1',
         'Options': {'low': 'Низкий', 'high': 'Высокий'}},
        {'Id': 'summary', 'Type': 'text', 'Name': 'Резюме', 'Required': '1'},
        {'Id': 'urgent', 'Type': 'bool', 'Name': 'Срочно'}]},
}


class BPBitrix(FakeBitrix):
    def __init__(self, tasks):
        super().__init__()
        self.tasks = tasks

    def respond(self, method, params):
        if method == 'bizproc.task.list':
            return list(self.tasks)
        if method == 'crm.item.get':
            return {'item': {'id': params['id'], 'title': 'Объект', 'stageId': 'NEW', 'categoryId': 0}}
        return super().respond(method, params)


async def make(settings, tasks, answers, **over):  # noqa: F811
    s = replace(settings, bp_agent=True, **over)
    b24 = BPBitrix(tasks)
    store = Store(s.data_dir)
    await store.set('bot_id_v2', BOT_ID)
    imbot = ImBotV2(b24, s, store)
    await imbot.load()
    tb = build_toolbox()
    llm = FakeLLM(answers, s)
    return BPParticipant(b24, s, store, EntityRegistry(b24), tb, Agent(llm, tb), imbot), b24, llm


def test_parse_document_and_fields():
    assert parse_document(APPROVE_TASK) == ('DEAL', '12')
    assert parse_document(INFO_TASK) == ('DYNAMIC_1038', '5')
    assert parse_document({'DOCUMENT_ID': ['lists', 'BizprocDocument', '777']}) == ('lists', '777')
    assert parse_document({'DOCUMENT_URL': '/crm/type/1038/details/9/'}) == ('DYNAMIC_1038', '9')
    f = {x['id']: x for x in task_fields(INFO_TASK)}
    assert f['risk']['required'] and f['risk']['type'] == 'select'
    assert coerce(f['risk'], 'Высокий') == 'high' and coerce(f['urgent'], 'да') == 'Y'
    with pytest.raises(ValueError):
        coerce(f['risk'], 'средний')


async def test_approve_task_completed_readonly_with_comment(settings):  # noqa: F811
    bp, b24, llm = await make(settings, [APPROVE_TASK], [
        {'content': '{"decision": "approve", "comment": "Срок оплаты 30 дней — соответствует.", "confidence": 0.9}'}])
    assert await bp.poll_once() == 1
    done = [p for m, p in b24.calls if m == 'bizproc.task.complete']
    assert done == [{'TASK_ID': 41, 'STATUS': 1, 'COMMENT': 'ИИ-агент: Срок оплаты 30 дней — соответствует.'}]
    offered = {t['function']['name'] for t in llm.seen[0][1]}
    assert 'crm_files' in offered and 'crm_update' not in offered and 'bp_task_complete' not in offered
    assert 'Проверь договор во вложении' in llm.seen[0][0][-1]['content']  # инструкция из задания в промпте
    assert any(m == 'crm.timeline.comment.add' for m, _ in b24.calls)  # отметка в таймлайне сделки
    assert await bp.poll_once() == 0  # повторно не берётся


async def test_request_information_fields_validated_and_retried(settings):  # noqa: F811
    bp, b24, _ = await make(settings, [INFO_TASK], [
        {'content': '{"decision": "ok", "comment": "Оценил", "fields": {"risk": "средний"}}'},  # нет такого варианта
        {'content': '{"decision": "ok", "comment": "Риск высокий: нет предоплаты.", '
                    '"fields": {"risk": "Высокий", "summary": "Нет предоплаты", "urgent": true}}'}])
    await bp.poll_once()
    done = [p for m, p in b24.calls if m == 'bizproc.task.complete']
    assert len(done) == 1
    assert done[0]['STATUS'] == 3 and done[0]['FIELDS'] == {'risk': 'high', 'summary': 'Нет предоплаты', 'urgent': 'Y'}


async def test_not_allowed_decision_escalates_and_delegates(settings):  # noqa: F811
    bp, b24, _ = await make(settings, [APPROVE_TASK], [
        {'content': '{"decision": "reject", "comment": "Оплата 60 дней — нарушение.", "confidence": 0.95}'}],
        bp_decisions=frozenset({'approve', 'ok'}), bp_escalate_to=7)
    await bp.poll_once()
    assert not [1 for m, _ in b24.calls if m == 'bizproc.task.complete']
    delegate = [p for m, p in b24.calls if m == 'bizproc.task.delegate']
    assert delegate == [{'TASK_IDS': [41], 'FROM_USER_ID': 1, 'TO_USER_ID': 7}]
    msg = [p for m, p in b24.calls if m == 'imbot.v2.Chat.Message.send']
    assert msg[0]['dialogId'] == '7' and 'Оплата 60 дней' in msg[0]['fields']['message']


async def test_low_confidence_and_model_escalate(settings):  # noqa: F811
    bp, b24, _ = await make(settings, [APPROVE_TASK], [
        {'content': '{"decision": "approve", "comment": "Вроде ок", "confidence": 0.2}'}])
    await bp.poll_once()
    assert not [1 for m, _ in b24.calls if m == 'bizproc.task.complete']
    bp2, b24b, _ = await make(replace(settings, data_dir=settings.data_dir / 'x'), [APPROVE_TASK], [
        {'content': '{"decision": "escalate", "comment": "Во вложении нет договора"}'}])
    await bp2.poll_once()
    assert not [1 for m, _ in b24b.calls if m == 'bizproc.task.complete']
    assert await bp2.store.get('bp_done:41') == 'escalated'
