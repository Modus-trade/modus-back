"""Observed service outcomes, with equal periods and explicit sample sizes."""
from datetime import timedelta
from statistics import median
from sqlalchemy import select
from ..models import Ticket, Comment, User, now
from .tickets import visible


def report(db,user,days):
    end=now();start=end-timedelta(days=days);previous=start-timedelta(days=days)
    tickets=db.scalars(visible(user)).all()
    ids=[t.id for t in tickets]
    comments=db.scalars(select(Comment).where(Comment.ticket_id.in_(ids),Comment.created_at>=previous)).all() if ids else []
    users={u.id:u for u in db.scalars(select(User))}
    def period(a,b):
        created=[t for t in tickets if a<=t.created_at<b]
        responses=[t for t in tickets if t.first_response_at and a<=t.first_response_at<b and t.first_response_at>=t.created_at]
        solved=[t for t in tickets if t.resolution_confirmed_at and a<=t.resolution_confirmed_at<b and t.resolution_confirmed_at>=t.created_at]
        actors={t.author_id for t in created}|{c.author_id for c in comments if a<=c.created_at<b}
        return {'created':len(created),'confirmed':len(solved),'active_people':len(actors),
            'response_hours':round(median([(t.first_response_at-t.created_at).total_seconds()/3600 for t in responses]),1) if responses else None,
            'response_sample':len(responses),
            'solution_hours':round(median([(t.resolution_confirmed_at-t.created_at).total_seconds()/3600 for t in solved]),1) if solved else None,
            'solution_sample':len(solved),
            'on_time_percent':round(100*sum(t.first_response_at<=t.due_at for t in responses)/len(responses),1) if responses else None}
    weeks=[];cursor=end-timedelta(days=84)
    while cursor<end:
        stop=min(cursor+timedelta(days=7),end)
        weeks.append({'start':cursor.isoformat(),'end':stop.isoformat(),**period(cursor,stop)})
        cursor=stop
    open_tickets=[t for t in tickets if t.status not in ('CLOSED','REJECTED')]
    stale=[t for t in open_tickets if t.updated_at<end-timedelta(days=14)]
    overdue=[t for t in open_tickets if not t.first_response_at and t.due_at<end]
    current=period(start,end);prev=period(previous,start)
    first=min((t.created_at for t in tickets),default=None)
    teams={}
    for t in tickets:
        department=users[t.author_id].department if t.author_id in users else 'Не указано'
        row=teams.setdefault(department,{'department':department,'created':0,'confirmed':0,'open':0})
        row['created']+=int(start<=t.created_at<end)
        row['confirmed']+=int(bool(t.resolution_confirmed_at and start<=t.resolution_confirmed_at<end))
        row['open']+=int(t.status not in ('CLOSED','REJECTED'))
    results=sorted([t for t in tickets if t.resolution_confirmed_at and start<=t.resolution_confirmed_at<end],key=lambda t:t.resolution_confirmed_at,reverse=True)
    return {'generated_at':end,'current_start':start,'previous_start':previous,'first_record_at':first,
        'comparison_ready':bool(first and first<=previous),'scope':'Вся компания' if user.role=='ADMIN' else 'Доступные вам обращения',
        'current':current,'previous':prev,'weeks':weeks,'backlog':len(open_tickets),'stale':len(stale),'overdue':len(overdue),
        'teams':sorted(teams.values(),key=lambda r:-r['created']),
        'results':[{'id':t.id,'number':t.number,'title':t.title,'resolution':t.resolution,'confirmed_at':t.resolution_confirmed_at} for t in results[:10]]}
